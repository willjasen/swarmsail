use aes_gcm::{
    aead::{Aead, AeadCore, KeyInit, OsRng, Payload},
    Aes256Gcm, Nonce,
};
use axum::{
    extract::{DefaultBodyLimit, Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use base64::{engine::general_purpose::STANDARD, Engine};
use chrono::{SecondsFormat, TimeZone, Utc};
use fs2::FileExt;
use jsonwebtoken::{decode, decode_header, Algorithm, DecodingKey, Validation};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    net::SocketAddr,
    path::{Path as FsPath, PathBuf},
    process::{Command, Stdio},
    sync::Arc,
    thread,
    time::Duration,
};
use tokio::sync::Semaphore;
use uuid::Uuid;
use wait_timeout::ChildExt;

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS provisioning_version(version INTEGER NOT NULL);
INSERT INTO provisioning_version SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM provisioning_version);
CREATE TABLE IF NOT EXISTS account_defaults(
 account TEXT PRIMARY KEY, tailnet TEXT NOT NULL UNIQUE, document TEXT NOT NULL,
 spec TEXT, due INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS requests(
 account TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
 resource TEXT NOT NULL, PRIMARY KEY(account,key));
CREATE TABLE IF NOT EXISTS enrollments(
 id TEXT PRIMARY KEY, account TEXT NOT NULL REFERENCES account_defaults(account),
 node TEXT NOT NULL, intent TEXT NOT NULL, document TEXT NOT NULL,
 cipher TEXT, due INTEGER NOT NULL DEFAULT 1, predecessor TEXT UNIQUE,
 verified INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS worker_receipts(
 resource TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS provisioning_authority(
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),issuer TEXT NOT NULL,service TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS node_mappings(
 account TEXT NOT NULL REFERENCES account_defaults(account), node TEXT NOT NULL,
 document TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(account,node));
CREATE TRIGGER IF NOT EXISTS default_identity_immutable
 BEFORE UPDATE OF account,tailnet ON account_defaults
 BEGIN SELECT RAISE(ABORT,'default identity is immutable'); END;
"#;

#[derive(Debug)]
pub struct Error {
    pub code: &'static str,
    status: StatusCode,
    retryable: bool,
    enrollment_id: Option<String>,
    successor_enrollment_id: Option<String>,
    recovery: Option<Value>,
    recovery_request_id: Option<Value>,
}
impl Error {
    fn new(status: StatusCode, code: &'static str) -> Self {
        Self {
            code,
            status,
            retryable: status == StatusCode::SERVICE_UNAVAILABLE,
            enrollment_id: None,
            successor_enrollment_id: None,
            recovery: None,
            recovery_request_id: None,
        }
    }
    fn for_enrollment(mut self, id: &str) -> Self {
        self.enrollment_id = Some(id.to_owned());
        self
    }
    fn for_recovery(mut self, d: &Value) -> Self {
        if d["recovery"].is_object() {
            self.recovery = Some(d["recovery"].clone());
            self.recovery_request_id = Some(d["recovery_request_id"].clone());
        }
        self
    }
    fn storage(_: impl std::fmt::Debug) -> Self {
        eprintln!("provisioning storage unavailable");
        Self::new(StatusCode::SERVICE_UNAVAILABLE, "storage_unavailable")
    }
}
impl IntoResponse for Error {
    fn into_response(self) -> Response {
        let mut body = json!({
            "api_version":"1", "error":{"code":self.code,"retryable":self.retryable},
            "operation_id":null
        });
        if let Some(id) = self.enrollment_id {
            body["enrollment_id"] = json!(id);
        }
        if let Some(id) = self.successor_enrollment_id {
            body["successor_enrollment_id"] = json!(id);
        }
        if let Some(recovery) = self.recovery {
            body["recovery"] = recovery;
            body["recovery_request_id"] = self.recovery_request_id.unwrap_or(Value::Null);
        }
        response(self.status, body, None)
    }
}
type Result<T> = std::result::Result<T, Error>;

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct Settings {
    ledger: PathBuf,
    issuer: String,
    service: String,
    listen: SocketAddr,
    public_keys: HashMap<String, PathBuf>,
    receipt_key_file: PathBuf,
    tenant_capacity: usize,
    #[serde(default)]
    worker: Option<WorkerSettings>,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct WorkerSettings {
    repository: PathBuf,
    settings: PathBuf,
}
#[derive(Clone)]
struct App {
    settings: Settings,
    keys: Arc<HashMap<String, DecodingKey>>,
    cipher: Arc<Aes256Gcm>,
    admission: Arc<Semaphore>,
}
#[derive(Debug, Deserialize, Serialize)]
struct Claims {
    iss: String,
    aud: String,
    azp: String,
    sub: String,
    scope: Vec<String>,
    iat: i64,
    nbf: i64,
    exp: i64,
    jti: String,
    node_id: Option<String>,
    setup_intent_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    recovery: Option<RecoveryBinding>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    recovery_request_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    recovery_lookup: Option<RecoveryLookup>,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RecoveryLookup {
    version: String,
    original_request_id: String,
    node_id: String,
    setup_intent_id: String,
    owner_authorization_id: String,
}
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct RecoveryBinding {
    version: String,
    predecessor_enrollment_id: String,
    predecessor_setup_intent_id: String,
    setup_intent_id: String,
    node_id: String,
    owner_authorization_id: String,
    setup_evidence_sha256: String,
    account_root_fingerprint: String,
    role: String,
    capability_digest: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Empty {}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct EnrollmentRequest {
    node_id: String,
    setup_intent_id: String,
}
fn json_body<T>(
    body: std::result::Result<Json<T>, axum::extract::rejection::JsonRejection>,
) -> Result<T> {
    body.map(|Json(value)| value).map_err(|rejection| {
        if rejection.status() == StatusCode::PAYLOAD_TOO_LARGE {
            Error::new(StatusCode::PAYLOAD_TOO_LARGE, "body_too_large")
        } else {
            Error::new(StatusCode::BAD_REQUEST, "invalid_json")
        }
    })
}
fn now() -> i64 {
    Utc::now().timestamp()
}
fn stamp(epoch: i64) -> Value {
    Utc.timestamp_opt(epoch, 0)
        .single()
        .map(|t| json!(t.to_rfc3339_opts(SecondsFormat::Secs, true)))
        .unwrap_or(Value::Null)
}
fn uuid(value: &str) -> Result<()> {
    if Uuid::parse_str(value)
        .map(|u| u.to_string() == value)
        .unwrap_or(false)
    {
        Ok(())
    } else {
        Err(Error::new(StatusCode::BAD_REQUEST, "invalid_uuid"))
    }
}
fn node(value: &str) -> bool {
    value.len() == 31
        && value.starts_with("fossil-")
        && value.as_bytes()[7..]
            .iter()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
}
fn secure_file(path: &FsPath) -> Result<Vec<u8>> {
    use std::os::unix::fs::PermissionsExt;
    let metadata = fs::symlink_metadata(path).map_err(Error::storage)?;
    if !metadata.is_file() || metadata.permissions().mode() & 0o077 != 0 {
        return Err(Error::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "private_file_permissions",
        ));
    }
    fs::read(path).map_err(Error::storage)
}
impl App {
    fn load(settings: Settings) -> Result<Self> {
        if !settings.ledger.is_absolute()
            || !settings.listen.ip().is_loopback()
            || settings.issuer.is_empty()
            || settings.service.is_empty()
            || settings.tenant_capacity == 0
        {
            return Err(Error::new(StatusCode::BAD_REQUEST, "invalid_settings"));
        }
        secure_file(&settings.ledger)?;
        let key = secure_file(&settings.receipt_key_file)?;
        let cipher = Aes256Gcm::new_from_slice(&key)
            .map_err(|_| Error::new(StatusCode::BAD_REQUEST, "invalid_receipt_key"))?;
        let mut keys = HashMap::new();
        for (kid, path) in &settings.public_keys {
            let bytes = fs::read(path).map_err(Error::storage)?;
            let key = DecodingKey::from_ed_pem(&bytes)
                .map_err(|_| Error::new(StatusCode::BAD_REQUEST, "invalid_public_key"))?;
            keys.insert(kid.clone(), key);
        }
        if keys.is_empty() {
            return Err(Error::new(StatusCode::BAD_REQUEST, "missing_public_keys"));
        }
        let app = Self {
            settings,
            keys: Arc::new(keys),
            cipher: Arc::new(cipher),
            admission: Arc::new(Semaphore::new(8)),
        };
        let db = app.db()?;
        let version_exists: bool = db
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='provisioning_version')",
                [],
                |r| r.get(0),
            )
            .map_err(Error::storage)?;
        if version_exists {
            let versions: Vec<i64> = db
                .prepare("SELECT version FROM provisioning_version")
                .map_err(Error::storage)?
                .query_map([], |r| r.get(0))
                .map_err(Error::storage)?
                .collect::<std::result::Result<_, _>>()
                .map_err(Error::storage)?;
            if versions != [1] {
                return Err(Error::new(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "ledger_version_unsupported",
                ));
            }
        }
        db.execute_batch(SCHEMA).map_err(Error::storage)?;
        db.execute(
            "INSERT OR IGNORE INTO provisioning_authority VALUES(1,?1,?2)",
            params![app.settings.issuer, app.settings.service],
        )
        .map_err(Error::storage)?;
        let authority: (String, String) = db
            .query_row(
                "SELECT issuer,service FROM provisioning_authority WHERE singleton=1",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .map_err(Error::storage)?;
        if authority != (app.settings.issuer.clone(), app.settings.service.clone()) {
            return Err(Error::new(
                StatusCode::CONFLICT,
                "authority_binding_conflict",
            ));
        }
        let versions: Vec<i64> = db
            .prepare("SELECT version FROM provisioning_version")
            .map_err(Error::storage)?
            .query_map([], |r| r.get(0))
            .map_err(Error::storage)?
            .collect::<std::result::Result<_, _>>()
            .map_err(Error::storage)?;
        if versions != [1] {
            return Err(Error::new(
                StatusCode::SERVICE_UNAVAILABLE,
                "ledger_version_unsupported",
            ));
        }
        Ok(app)
    }
    fn db(&self) -> Result<Connection> {
        let db = Connection::open(&self.settings.ledger).map_err(Error::storage)?;
        db.busy_timeout(Duration::from_secs(2))
            .map_err(Error::storage)?;
        db.execute_batch(
            "PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;",
        )
        .map_err(Error::storage)?;
        Ok(db)
    }
    fn authorize(&self, headers: &HeaderMap, account: &str, scope: &str) -> Result<Claims> {
        uuid(account)?;
        let denied = || Error::new(StatusCode::UNAUTHORIZED, "invalid_assertion");
        let token = headers
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.strip_prefix("Bearer "))
            .ok_or_else(denied)?;
        if token.len() > 4096 {
            return Err(denied());
        }
        let header = decode_header(token).map_err(|_| denied())?;
        if header.alg != Algorithm::EdDSA {
            return Err(denied());
        }
        let key = header
            .kid
            .as_ref()
            .and_then(|kid| self.keys.get(kid))
            .ok_or_else(denied)?;
        let mut validation = Validation::new(Algorithm::EdDSA);
        validation.set_issuer(&[&self.settings.issuer]);
        validation.set_audience(&[if scope.starts_with("node:recover") {
            "swarmsail-enrollment-recovery-v1"
        } else {
            "swarmsail-provisioning-v1"
        }]);
        validation.validate_nbf = true;
        validation.leeway = 30;
        let c = decode::<Claims>(token, key, &validation)
            .map_err(|_| denied())?
            .claims;
        if c.exp <= c.iat
            || c.exp - c.iat > 300
            || c.iat > now() + 30
            || c.nbf > c.exp
            || c.jti.is_empty()
            || c.jti.len() > 128
        {
            return Err(denied());
        }
        if c.sub != account || c.azp != self.settings.service || !c.scope.iter().any(|s| s == scope)
        {
            return Err(Error::new(
                StatusCode::FORBIDDEN,
                "account_or_scope_mismatch",
            ));
        }
        Ok(c)
    }
    fn encrypt(&self, key: &str, document: &Value) -> Result<String> {
        let nonce = Aes256Gcm::generate_nonce(&mut OsRng);
        let aad = associated(document, &self.settings.issuer)?;
        let encrypted = self
            .cipher
            .encrypt(
                &nonce,
                Payload {
                    msg: key.as_bytes(),
                    aad: &aad,
                },
            )
            .map_err(|_| {
                Error::new(StatusCode::SERVICE_UNAVAILABLE, "receipt_encryption_failed")
            })?;
        Ok(format!(
            "{}.{}",
            STANDARD.encode(nonce),
            STANDARD.encode(encrypted)
        ))
    }
    fn decrypt(&self, ciphertext: &str, document: &Value) -> Result<String> {
        let failed = || Error::new(StatusCode::CONFLICT, "enrollment_uncertain");
        let (nonce, data) = ciphertext.split_once('.').ok_or_else(failed)?;
        let nonce = STANDARD.decode(nonce).map_err(|_| failed())?;
        if nonce.len() != 12 {
            return Err(failed());
        }
        let data = STANDARD.decode(data).map_err(|_| failed())?;
        let aad = associated(document, &self.settings.issuer)?;
        let plain = self
            .cipher
            .decrypt(
                Nonce::from_slice(&nonce),
                Payload {
                    msg: &data,
                    aad: &aad,
                },
            )
            .map_err(|_| failed())?;
        String::from_utf8(plain).map_err(|_| failed())
    }
}
fn associated(d: &Value, authority: &str) -> Result<Vec<u8>> {
    serde_json::to_vec(&json!([
        authority,
        d["account_id"],
        d["node_id"],
        d["setup_intent_id"],
        d["enrollment_id"],
        d["tailnet_id"],
        d["headscale_key_id"],
        d["expires_at"]
    ]))
    .map_err(Error::storage)
}
fn document(db: &Connection, account: &str) -> Result<Value> {
    let value: Option<(String, Option<String>)> = db
        .query_row(
            "SELECT document,spec FROM account_defaults WHERE account=?1",
            [account],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .map_err(Error::storage)?;
    let (value, spec) =
        value.ok_or_else(|| Error::new(StatusCode::NOT_FOUND, "tailnet_not_found"))?;
    let mut d: Value = serde_json::from_str(&value).map_err(Error::storage)?;
    d["enrollment_recovery_version"] = json!("1");
    if let Some(spec) = spec {
        let spec: Value = serde_json::from_str(&spec).map_err(Error::storage)?;
        d["node_address_policy"] =
            json!({"version":"1","ipv4_prefix":spec["ipv4_prefix"],"ipv6_prefix":null});
    }
    if d["state"] == "ready" && d["valid_until_epoch"].as_i64().unwrap_or(0) <= now() {
        d["state"] = json!("degraded");
        d["error"] = json!({"code":"readiness_stale","retryable":true});
    }
    Ok(d)
}
fn external(mut d: Value) -> Value {
    if let Some(map) = d.as_object_mut() {
        map.remove("valid_until_epoch");
        map.remove("headscale_key_id");
        map.remove("delivered");
        map.remove("verified_epoch");
        map.remove("creation_started");
        map.remove("receipt_key");
        if map.get("state") == Some(&json!("issuing")) {
            map.remove("expires_at");
            map.remove("server_url");
            map.remove("reusable");
            map.remove("max_uses");
            map.remove("account_id");
            map.remove("node_address_policy");
        }
    }
    d
}
fn response(status: StatusCode, d: Value, location: Option<String>) -> Response {
    let mut r = (status, Json(external(d))).into_response();
    r.headers_mut()
        .insert("cache-control", "no-store".parse().unwrap());
    if status == StatusCode::ACCEPTED
        || status == StatusCode::SERVICE_UNAVAILABLE
        || status == StatusCode::TOO_MANY_REQUESTS
    {
        r.headers_mut().insert("retry-after", "5".parse().unwrap());
    }
    if let Some(location) = location {
        if let Ok(value) = location.parse() {
            r.headers_mut().insert("location", value);
        }
    }
    r
}
fn request_key(h: &HeaderMap) -> Result<String> {
    let key = h
        .get("idempotency-key")
        .and_then(|v| v.to_str().ok())
        .ok_or_else(|| Error::new(StatusCode::BAD_REQUEST, "idempotency_key_required"))?;
    uuid(key)?;
    Ok(key.to_owned())
}
fn fingerprint(path: &str, body: &Value) -> String {
    format!("{:x}", Sha256::digest(format!("{path}:{body}").as_bytes()))
}
fn previous(db: &Connection, account: &str, key: &str, fp: &str) -> Result<Option<String>> {
    let row: Option<(String, String)> = db
        .query_row(
            "SELECT fingerprint,resource FROM requests WHERE account=?1 AND key=?2",
            params![account, key],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .map_err(Error::storage)?;
    match row {
        Some((old, id)) if old == fp => Ok(Some(id)),
        Some(_) => Err(Error::new(StatusCode::CONFLICT, "idempotency_conflict")),
        None => Ok(None),
    }
}
fn remember(db: &Connection, account: &str, key: &str, fp: &str, resource: &str) -> Result<()> {
    db.execute(
        "INSERT OR IGNORE INTO requests VALUES(?1,?2,?3,?4)",
        params![account, key, fp, resource],
    )
    .map_err(Error::storage)?;
    Ok(())
}
fn scope_node(c: &Claims, node_id: &str, intent: &str) -> Result<()> {
    uuid(intent)?;
    if !node(node_id) {
        return Err(Error::new(StatusCode::BAD_REQUEST, "invalid_node_id"));
    }
    if c.node_id.as_deref() != Some(node_id) || c.setup_intent_id.as_deref() != Some(intent) {
        return Err(Error::new(StatusCode::FORBIDDEN, "node_claim_mismatch"));
    }
    Ok(())
}
async fn ensure(
    State(app): State<App>,
    Path((account, action)): Path<(String, String)>,
    h: HeaderMap,
    body: std::result::Result<Json<Empty>, axum::extract::rejection::JsonRejection>,
) -> Result<Response> {
    if action != "default-tailnet:ensure" {
        return Err(Error::new(StatusCode::NOT_FOUND, "not_found"));
    }
    app.authorize(&h, &account, "tailnet:ensure")?;
    json_body(body)?;
    let key = request_key(&h)?;
    let fp = fingerprint("ensure", &json!({}));
    let mut db = app.db()?;
    let tx = db
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(Error::storage)?;
    previous(&tx, &account, &key, &fp)?;
    let existing: Option<String> = tx
        .query_row(
            "SELECT tailnet FROM account_defaults WHERE account=?1",
            [&account],
            |r| r.get(0),
        )
        .optional()
        .map_err(Error::storage)?;
    let tailnet = if let Some(id) = existing {
        id
    } else {
        let count: usize = tx
            .query_row("SELECT COUNT(*) FROM account_defaults", [], |r| r.get(0))
            .map_err(Error::storage)?;
        let queued: usize = tx
            .query_row(
                "SELECT COUNT(*) FROM account_defaults WHERE due=1",
                [],
                |r| r.get(0),
            )
            .map_err(Error::storage)?;
        if count >= app.settings.tenant_capacity || queued >= 32 {
            return Err(Error::new(
                StatusCode::SERVICE_UNAVAILABLE,
                "capacity_unavailable",
            ));
        }
        let id = Uuid::new_v4().to_string();
        let d = json!({
            "api_version":"1","account_id":account,"tailnet_id":id,"generation":1,
            "operation_id":Uuid::new_v4().to_string(),"state":"queued","phase":"allocation",
            "server_url":format!("https://{id}.swarm.rawrarr.net"),
            "readiness":{"checked_at":null,"valid_until":null},"error":null
        });
        tx.execute(
            "INSERT INTO account_defaults(account,tailnet,document,due) VALUES(?1,?2,?3,1)",
            params![account, id, d.to_string()],
        )
        .map_err(Error::storage)?;
        id
    };
    remember(&tx, &account, &key, &fp, &tailnet)?;
    tx.execute(
        "UPDATE account_defaults SET due=1 WHERE account=?1",
        [&account],
    )
    .map_err(Error::storage)?;
    let d = document(&tx, &account)?;
    tx.commit().map_err(Error::storage)?;
    Ok(response(
        if d["state"] == "ready" {
            StatusCode::OK
        } else {
            StatusCode::ACCEPTED
        },
        d,
        Some(format!("/private/v1/accounts/{account}/default-tailnet")),
    ))
}
async fn read_default(
    State(app): State<App>,
    Path(account): Path<String>,
    h: HeaderMap,
) -> Result<Response> {
    app.authorize(&h, &account, "tailnet:read")?;
    Ok(response(
        StatusCode::OK,
        document(&app.db()?, &account)?,
        None,
    ))
}
fn enrollment(db: &Connection, account: &str, id: &str) -> Result<Value> {
    let value: Option<String> = db
        .query_row(
            "SELECT document FROM enrollments WHERE account=?1 AND id=?2",
            params![account, id],
            |r| r.get(0),
        )
        .optional()
        .map_err(Error::storage)?;
    serde_json::from_str(
        &value.ok_or_else(|| Error::new(StatusCode::NOT_FOUND, "enrollment_not_found"))?,
    )
    .map_err(Error::storage)
}
fn create_enrollment(
    db: &Connection,
    account: &str,
    req: &EnrollmentRequest,
    predecessor: Option<&str>,
) -> Result<String> {
    let d = document(db, account)?;
    if d["state"] != "ready" {
        return Err(Error::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "tailnet_not_ready",
        ));
    }
    if predecessor.is_none() {
        let prior: bool = db
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM enrollments WHERE account=?1 AND node=?2)",
                params![account, req.node_id],
                |r| r.get(0),
            )
            .map_err(Error::storage)?;
        if prior {
            return Err(Error::new(StatusCode::CONFLICT, "node_enrollment_exists"));
        }
    }
    let active: i64 = db.query_row(
        "SELECT COUNT(*) FROM enrollments WHERE account=?1 AND node=?2 AND json_extract(document,'$.state') IN ('issuing','issued','uncertain')",
        params![account,req.node_id], |r| r.get(0)
    ).map_err(Error::storage)?;
    let predecessor_active: i64 = if let Some(id) = predecessor {
        db.query_row("SELECT COUNT(*) FROM enrollments WHERE id=?1 AND json_extract(document,'$.state') IN ('issuing','issued','uncertain')", [id], |r| r.get(0)).map_err(Error::storage)?
    } else {
        0
    };
    if active > predecessor_active {
        return Err(Error::new(StatusCode::CONFLICT, "node_enrollment_exists"));
    }
    let queued: i64 = db
        .query_row("SELECT COUNT(*) FROM enrollments WHERE due!=0", [], |r| {
            r.get(0)
        })
        .map_err(Error::storage)?;
    if queued >= 32 {
        return Err(Error::new(StatusCode::TOO_MANY_REQUESTS, "admission_busy"));
    }
    let id = Uuid::new_v4().to_string();
    let e = json!({
        "api_version":"1","account_id":account,"enrollment_id":id,"node_id":req.node_id,
        "setup_intent_id":req.setup_intent_id,"tailnet_id":d["tailnet_id"],
        "server_url":d["server_url"],"state":"issuing","expires_at":null,
        "node_address_policy":d["node_address_policy"],
        "reusable":false,"max_uses":1,"error":null,"delivered":false
    });
    db.execute("INSERT INTO enrollments(id,account,node,intent,document,predecessor) VALUES(?1,?2,?3,?4,?5,?6)", params![id,account,req.node_id,req.setup_intent_id,e.to_string(),predecessor]).map_err(Error::storage)?;
    Ok(id)
}
async fn read_node(
    State(app): State<App>,
    Path((account, node_id)): Path<(String, String)>,
    h: HeaderMap,
) -> Result<Response> {
    let c = app.authorize(&h, &account, "node:read")?;
    if !node(&node_id) {
        return Err(Error::new(StatusCode::BAD_REQUEST, "invalid_node_id"));
    }
    if c.node_id.as_deref() != Some(&node_id) {
        return Err(Error::new(StatusCode::FORBIDDEN, "node_claim_mismatch"));
    }
    let db = app.db()?;
    let default = document(&db, &account)?;
    let count: i64 = db
        .query_row(
            "SELECT COUNT(*) FROM enrollments WHERE account=?1 AND node=?2",
            params![account, node_id],
            |r| r.get(0),
        )
        .map_err(Error::storage)?;
    if count == 0 {
        return Err(Error::new(StatusCode::NOT_FOUND, "node_not_found"));
    }
    let pending = json!({
        "api_version":"1","account_id":account,"tailnet_id":default["tailnet_id"],
        "node_id":node_id,"state":"pending","revision":0,"checked_at":null,"valid_until":null,
        "enrollment_id":null,"setup_intent_id":null,"device":null,
        "error":{"code":"node_mapping_pending","retryable":true}
    });
    db.execute(
        "INSERT OR IGNORE INTO node_mappings(account,node,document) VALUES(?1,?2,?3)",
        params![account, node_id, pending.to_string()],
    )
    .map_err(Error::storage)?;
    let (text, verified): (String, i64) = db
        .query_row(
            "SELECT document,verified FROM node_mappings WHERE account=?1 AND node=?2",
            params![account, node_id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .map_err(Error::storage)?;
    let d: Value = serde_json::from_str(&text).map_err(Error::storage)?;
    if verified > now() - 5 && default["state"] == "ready" && d["state"] != "pending" {
        Ok(response(StatusCode::OK, d, None))
    } else {
        let mut pending = pending;
        pending["revision"] = d["revision"].clone();
        Ok(response(
            StatusCode::ACCEPTED,
            pending,
            Some(format!(
                "/private/v1/accounts/{account}/default-tailnet/nodes/{node_id}"
            )),
        ))
    }
}
async fn issue(
    State(app): State<App>,
    Path(account): Path<String>,
    h: HeaderMap,
    body: std::result::Result<Json<EnrollmentRequest>, axum::extract::rejection::JsonRejection>,
) -> Result<Response> {
    let c = app.authorize(&h, &account, "node:enroll")?;
    let req = json_body(body)?;
    scope_node(&c, &req.node_id, &req.setup_intent_id)?;
    let key = request_key(&h)?;
    let fp = fingerprint(
        "enroll",
        &serde_json::to_value(&req).map_err(Error::storage)?,
    );
    let id = {
        let mut db = app.db()?;
        let tx = db
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(Error::storage)?;
        let id = if let Some(id) = previous(&tx, &account, &key, &fp)? {
            id
        } else {
            create_enrollment(&tx, &account, &req, None)?
        };
        remember(&tx, &account, &key, &fp, &id)?;
        tx.execute(
            "UPDATE enrollments SET due=1,verified=0 WHERE id=?1 AND (due!=0 OR verified<=?2)",
            params![id, now() - 5],
        )
        .map_err(Error::storage)?;
        tx.commit().map_err(Error::storage)?;
        id
    };
    receipt(&app, &account, &id).await
}
async fn receipt(app: &App, account: &str, id: &str) -> Result<Response> {
    // Wait only for the supervised worker's authoritative check, never run Docker here.
    for _ in 0..20 {
        {
            let mut db = app.db()?;
            let tx = db
                .transaction_with_behavior(TransactionBehavior::Immediate)
                .map_err(Error::storage)?;
            let mut d = enrollment(&tx, account, id)?;
            let successor: Option<String> = tx.query_row(
                "SELECT id FROM enrollments WHERE predecessor=?1 AND json_extract(document,'$.recovery') IS NOT NULL",
                [id], |r| r.get(0),
            ).optional().map_err(Error::storage)?;
            if let Some(successor) = successor {
                let mut error =
                    Error::new(StatusCode::CONFLICT, "enrollment_superseded").for_enrollment(id);
                error.successor_enrollment_id = Some(successor);
                return Err(error);
            }
            let (due, verified, cipher): (i64, i64, Option<String>) = tx
                .query_row(
                    "SELECT due,verified,cipher FROM enrollments WHERE id=?1",
                    [id],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                )
                .map_err(Error::storage)?;
            if due == 0 && verified > now() - 5 {
                if d["state"] != "issued" {
                    return Err(Error::new(
                        StatusCode::CONFLICT,
                        match d["state"].as_str() {
                            Some("consumed") => "enrollment_consumed",
                            Some("expired") => "enrollment_expired",
                            Some("revoked") => "enrollment_revoked",
                            _ => "enrollment_uncertain",
                        },
                    )
                    .for_enrollment(id)
                    .for_recovery(&d));
                }
                if document(&tx, account)?["state"] != "ready" {
                    return Err(Error::new(
                        StatusCode::SERVICE_UNAVAILABLE,
                        "tailnet_not_ready",
                    ));
                }
                let key = app
                    .decrypt(
                        cipher.as_deref().ok_or_else(|| {
                            Error::new(StatusCode::CONFLICT, "enrollment_uncertain")
                                .for_enrollment(id)
                                .for_recovery(&d)
                        })?,
                        &d,
                    )
                    .map_err(|error| error.for_enrollment(id).for_recovery(&d))?;
                let status = if d["delivered"] == true {
                    StatusCode::OK
                } else {
                    StatusCode::CREATED
                };
                d["delivered"] = json!(true);
                tx.execute(
                    "UPDATE enrollments SET document=?1 WHERE id=?2",
                    params![d.to_string(), id],
                )
                .map_err(Error::storage)?;
                tx.commit().map_err(Error::storage)?;
                d["auth_key"] = json!(key);
                return Ok(response(status, d, None));
            }
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    let d = enrollment(&app.db()?, account, id)?;
    Ok(response(
        StatusCode::ACCEPTED,
        d,
        Some(format!(
            "/private/v1/accounts/{account}/default-tailnet/enrollments/{id}"
        )),
    ))
}
async fn read_enrollment(
    State(app): State<App>,
    Path((account, id)): Path<(String, String)>,
    h: HeaderMap,
) -> Result<Response> {
    let c = app.authorize(&h, &account, "node:enroll")?;
    uuid(&id)?;
    let db = app.db()?;
    let mut d = enrollment(&db, &account, &id)?;
    scope_node(
        &c,
        d["node_id"].as_str().unwrap_or(""),
        d["setup_intent_id"].as_str().unwrap_or(""),
    )?;
    db.execute("UPDATE enrollments SET due=1 WHERE id=?1 AND due=0", [&id])
        .map_err(Error::storage)?;
    let successor:Option<String>=db.query_row(
        "SELECT id FROM enrollments WHERE predecessor=?1 AND json_extract(document,'$.recovery') IS NOT NULL",
        [&id],|r|r.get(0)).optional().map_err(Error::storage)?;
    if let Some(successor) = successor {
        d["successor_enrollment_id"] = json!(successor);
    }
    Ok(response(StatusCode::OK, d, None))
}
async fn replace(
    State(app): State<App>,
    Path((account, id)): Path<(String, String)>,
    h: HeaderMap,
    body: std::result::Result<Json<Empty>, axum::extract::rejection::JsonRejection>,
) -> Result<Response> {
    let c = app.authorize(&h, &account, "node:enroll")?;
    json_body(body)?;
    let id = id
        .strip_suffix(":replace")
        .ok_or_else(|| Error::new(StatusCode::NOT_FOUND, "not_found"))?
        .to_owned();
    uuid(&id)?;
    let key = request_key(&h)?;
    let fp = fingerprint(&format!("replace/{id}"), &json!({}));
    let successor = {
        let mut db = app.db()?;
        let tx = db
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(Error::storage)?;
        let old = enrollment(&tx, &account, &id)?;
        let req = EnrollmentRequest {
            node_id: old["node_id"].as_str().unwrap_or("").to_owned(),
            setup_intent_id: old["setup_intent_id"].as_str().unwrap_or("").to_owned(),
        };
        scope_node(&c, &req.node_id, &req.setup_intent_id)?;
        let next = if let Some(next) = previous(&tx, &account, &key, &fp)? {
            next
        } else {
            let existing: Option<String> = tx
                .query_row(
                    "SELECT id FROM enrollments WHERE predecessor=?1",
                    [&id],
                    |r| r.get(0),
                )
                .optional()
                .map_err(Error::storage)?;
            match existing {
                Some(id) => {
                    if enrollment(&tx, &account, &id)?["recovery"].is_object() {
                        return Err(Error::new(
                            StatusCode::CONFLICT,
                            "recovery_binding_conflict",
                        ));
                    }
                    id
                }
                None => create_enrollment(&tx, &account, &req, Some(&id))?,
            }
        };
        remember(&tx, &account, &key, &fp, &next)?;
        tx.execute(
            "UPDATE enrollments SET due=1,verified=0 WHERE id=?1 AND (due!=0 OR verified<=?2)",
            params![next, now() - 5],
        )
        .map_err(Error::storage)?;
        tx.commit().map_err(Error::storage)?;
        next
    };
    receipt(&app, &account, &successor).await
}
async fn recover(
    State(app): State<App>,
    Path((account, path_id)): Path<(String, String)>,
    h: HeaderMap,
    body: std::result::Result<Json<RecoveryBinding>, axum::extract::rejection::JsonRejection>,
) -> Result<Response> {
    let c = app.authorize(&h, &account, "node:recover")?;
    let binding = json_body(body)?;
    let id = path_id
        .strip_suffix(":recover")
        .ok_or_else(|| Error::new(StatusCode::NOT_FOUND, "not_found"))?;
    let key = request_key(&h)?;
    for value in [
        id,
        key.as_str(),
        binding.predecessor_setup_intent_id.as_str(),
        binding.setup_intent_id.as_str(),
        binding.owner_authorization_id.as_str(),
    ] {
        uuid(value)?;
        if value == Uuid::nil().to_string() {
            return Err(Error::new(
                StatusCode::BAD_REQUEST,
                "invalid_recovery_binding",
            ));
        }
    }
    let hash_valid = |s: &str| {
        s.len() == 64
            && s.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    };
    if binding.version != "1"
        || binding.predecessor_enrollment_id != id
        || binding.setup_intent_id == binding.predecessor_setup_intent_id
        || !hash_valid(&binding.setup_evidence_sha256)
        || !hash_valid(&binding.account_root_fingerprint)
        || !hash_valid(&binding.capability_digest)
        || binding.role != "metadata"
    {
        return Err(Error::new(
            StatusCode::BAD_REQUEST,
            "invalid_recovery_binding",
        ));
    }
    scope_node(&c, &binding.node_id, &binding.setup_intent_id)?;
    if c.scope != ["node:recover"]
        || c.recovery.as_ref() != Some(&binding)
        || c.recovery_request_id.as_deref() != Some(key.as_str())
    {
        return Err(Error::new(StatusCode::FORBIDDEN, "recovery_claim_mismatch"));
    }
    let logical = serde_json::to_value(&binding).map_err(Error::storage)?;
    let fp = fingerprint(&format!("recover/{id}"), &logical);
    let successor = {
        let mut db = app.db()?;
        let tx = db
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(Error::storage)?;
        let old = enrollment(&tx, &account, id)?;
        if old["node_id"] != binding.node_id
            || old["setup_intent_id"] != binding.predecessor_setup_intent_id
        {
            return Err(Error::new(
                StatusCode::CONFLICT,
                "recovery_binding_conflict",
            ));
        }
        if old["recovery"].is_object()
            && old["recovery"]["account_root_fingerprint"] != binding.account_root_fingerprint
        {
            return Err(Error::new(
                StatusCode::CONFLICT,
                "recovery_binding_conflict",
            ));
        }
        let existing: Option<String> = tx
            .query_row(
                "SELECT id FROM enrollments WHERE predecessor=?1",
                [id],
                |r| r.get(0),
            )
            .optional()
            .map_err(Error::storage)?;
        let previous = previous(&tx, &account, &key, &fp)?;
        let next = if let Some(next) = previous.or(existing) {
            if enrollment(&tx, &account, &next)?["recovery"] != logical {
                return Err(Error::new(
                    StatusCode::CONFLICT,
                    "recovery_binding_conflict",
                ));
            }
            next
        } else {
            let next = create_enrollment(
                &tx,
                &account,
                &EnrollmentRequest {
                    node_id: binding.node_id.clone(),
                    setup_intent_id: binding.setup_intent_id.clone(),
                },
                Some(id),
            )?;
            let mut d = enrollment(&tx, &account, &next)?;
            d["recovery"] = logical.clone();
            d["recovery_request_id"] = json!(key);
            tx.execute(
                "UPDATE enrollments SET document=?1 WHERE id=?2",
                params![d.to_string(), next],
            )
            .map_err(Error::storage)?;
            // Fence receipt delivery atomically with the unique successor reservation.
            tx.execute("UPDATE enrollments SET cipher=NULL,due=0 WHERE id=?1", [id])
                .map_err(Error::storage)?;
            tx.execute("UPDATE node_mappings SET verified=0,document=json_set(document,'$.state','uncertain','$.device',NULL,'$.checked_at',NULL,'$.valid_until',NULL,'$.revision',json_extract(document,'$.revision')+1) WHERE account=?1 AND node=?2",params![account,binding.node_id]).map_err(Error::storage)?;
            next
        };
        remember(&tx, &account, &key, &fp, &next)?;
        tx.execute(
            "UPDATE enrollments SET due=1,verified=0 WHERE id=?1 AND (due!=0 OR verified<=?2)",
            params![next, now() - 5],
        )
        .map_err(Error::storage)?;
        tx.commit().map_err(Error::storage)?;
        next
    };
    receipt(&app, &account, &successor).await
}
async fn enrollment_action(
    State(app): State<App>,
    Path(path): Path<(String, String)>,
    h: HeaderMap,
    body: std::result::Result<Json<Value>, axum::extract::rejection::JsonRejection>,
) -> Result<Response> {
    let value = json_body(body)?;
    if path.1.ends_with(":recover") {
        let req = serde_json::from_value(value)
            .map_err(|_| Error::new(StatusCode::BAD_REQUEST, "invalid_json"))?;
        recover(State(app), Path(path), h, Ok(Json(req))).await
    } else {
        let req = serde_json::from_value(value)
            .map_err(|_| Error::new(StatusCode::BAD_REQUEST, "invalid_json"))?;
        replace(State(app), Path(path), h, Ok(Json(req))).await
    }
}
async fn recovery_lookup(
    State(app): State<App>,
    Path((account, key)): Path<(String, String)>,
    h: HeaderMap,
) -> Result<Response> {
    let c = app.authorize(&h, &account, "node:recover:lookup")?;
    let binding = c
        .recovery_lookup
        .as_ref()
        .ok_or_else(|| Error::new(StatusCode::FORBIDDEN, "recovery_claim_mismatch"))?;
    uuid(&key)?;
    uuid(&binding.owner_authorization_id)?;
    scope_node(&c, &binding.node_id, &binding.setup_intent_id)?;
    if c.scope != ["node:recover:lookup"]
        || binding.version != "1"
        || binding.original_request_id != key
        || key == Uuid::nil().to_string()
        || binding.owner_authorization_id == Uuid::nil().to_string()
    {
        return Err(Error::new(StatusCode::FORBIDDEN, "recovery_claim_mismatch"));
    }
    let db = app.db()?;
    let row: Option<(String, String)> = db
        .query_row(
            "SELECT fingerprint,resource FROM requests WHERE account=?1 AND key=?2",
            params![account, key],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .map_err(Error::storage)?;
    let (fp, id) =
        row.ok_or_else(|| Error::new(StatusCode::NOT_FOUND, "enrollment_request_not_found"))?;
    let expected = fingerprint(
        "enroll",
        &json!({"node_id":binding.node_id,"setup_intent_id":binding.setup_intent_id}),
    );
    if fp != expected {
        return Err(Error::new(
            StatusCode::CONFLICT,
            "recovery_binding_conflict",
        ));
    }
    let mut d = enrollment(&db, &account, &id)?;
    if d["node_id"] != binding.node_id || d["setup_intent_id"] != binding.setup_intent_id {
        return Err(Error::new(
            StatusCode::CONFLICT,
            "recovery_binding_conflict",
        ));
    }
    let successor:Option<String>=db.query_row(
        "SELECT id FROM enrollments WHERE predecessor=?1 AND json_extract(document,'$.recovery') IS NOT NULL",
        [&id],|r|r.get(0)).optional().map_err(Error::storage)?;
    if let Some(successor) = successor {
        d["successor_enrollment_id"] = json!(successor);
    }
    Ok(response(StatusCode::OK, d, None))
}
fn router(app: App) -> Router {
    Router::new()
        .route("/private/v1/accounts/:account/:action", post(ensure))
        .route(
            "/private/v1/accounts/:account/default-tailnet",
            get(read_default),
        )
        .route(
            "/private/v1/accounts/:account/default-tailnet/enrollments",
            post(issue),
        )
        .route(
            "/private/v1/accounts/:account/default-tailnet/enrollments/:id",
            get(read_enrollment).post(enrollment_action),
        )
        .route(
            "/private/v1/accounts/:account/default-tailnet/enrollment-requests/:key",
            get(recovery_lookup),
        )
        .route(
            "/private/v1/accounts/:account/default-tailnet/nodes/:node",
            get(read_node),
        )
        .fallback(|| async { Error::new(StatusCode::NOT_FOUND, "not_found") })
        .layer(DefaultBodyLimit::max(8192))
        .layer(axum::middleware::from_fn_with_state(app.clone(), admission))
        .with_state(app)
}
async fn admission(
    State(app): State<App>,
    request: axum::extract::Request,
    next: axum::middleware::Next,
) -> Response {
    let Ok(_permit) = app.admission.clone().try_acquire_owned() else {
        return Error::new(StatusCode::TOO_MANY_REQUESTS, "admission_busy").into_response();
    };
    let mut response = next.run(request).await;
    response
        .headers_mut()
        .insert("cache-control", "no-store".parse().unwrap());
    response
}

fn adapter(app: &App, mode: &str, input: &Value) -> Result<Value> {
    let w =
        app.settings.worker.as_ref().ok_or_else(|| {
            Error::new(StatusCode::SERVICE_UNAVAILABLE, "worker_settings_missing")
        })?;
    let mut child = Command::new("python3")
        .args(["-m", "scripts.provisioning_worker", mode, "--settings"])
        .arg(&w.settings)
        .arg("--ledger")
        .arg(&app.settings.ledger)
        .current_dir(&w.repository)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(Error::storage)?;
    let mut stdin = child
        .stdin
        .take()
        .ok_or_else(|| Error::new(StatusCode::SERVICE_UNAVAILABLE, "worker_io_failed"))?;
    let payload = serde_json::to_vec(input).map_err(Error::storage)?;
    stdin.write_all(&payload).map_err(Error::storage)?;
    drop(stdin);
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| Error::new(StatusCode::SERVICE_UNAVAILABLE, "worker_io_failed"))?;
    let reader = thread::spawn(move || {
        let mut output = Vec::new();
        stdout.take(65537).read_to_end(&mut output).map(|_| output)
    });
    let status = match child
        .wait_timeout(Duration::from_secs(180))
        .map_err(Error::storage)?
    {
        Some(s) => s,
        None => {
            // The adapter's Docker requests have their own shorter deadlines.
            child.kill().map_err(Error::storage)?;
            child.wait().map_err(Error::storage)?;
            let _ = reader.join();
            return Err(Error::new(
                StatusCode::SERVICE_UNAVAILABLE,
                "worker_outcome_unknown",
            ));
        }
    };
    let output = reader
        .join()
        .map_err(Error::storage)?
        .map_err(Error::storage)?;
    if output.len() > 65536 || !status.success() {
        return Err(Error::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "worker_outcome_unknown",
        ));
    }
    let result: Value = serde_json::from_slice(&output).map_err(Error::storage)?;
    Ok(result)
}
fn worker_tick(app: &App) -> Result<()> {
    let db = app.db()?;
    db.execute("UPDATE enrollments SET cipher=NULL WHERE cipher IS NOT NULL AND CAST(strftime('%s',json_extract(document,'$.expires_at')) AS INTEGER) <= ?1", [now()]).map_err(Error::storage)?;
    let accounts: Vec<String> = db.prepare("SELECT account FROM account_defaults WHERE due=1 OR COALESCE(json_extract(document,'$.valid_until_epoch'),0) < ?1")
        .map_err(Error::storage)?.query_map([now()+20],|r| r.get(0)).map_err(Error::storage)?
        .collect::<std::result::Result<_,_>>().map_err(Error::storage)?;
    for account in accounts {
        let mut d = document(&db, &account)?;
        d["state"] = json!("provisioning");
        db.execute(
            "UPDATE account_defaults SET document=?1 WHERE account=?2",
            params![d.to_string(), account],
        )
        .map_err(Error::storage)?;
        let result = adapter(app, "ensure", &d)?;
        d["phase"] = result["phase"].clone();
        if result["ok"] == true {
            d["state"] = json!("ready");
            d["phase"] = json!("complete");
            d["error"] = Value::Null;
            d["readiness"] = json!({"checked_at":stamp(now()),"valid_until":stamp(now()+60)});
            d["valid_until_epoch"] = json!(now() + 60);
        } else {
            d["state"] = json!("blocked");
            d["error"] = json!({"code":result["code"],"retryable":true});
            d["valid_until_epoch"] = json!(now() + 30);
            d["readiness"] = json!({"checked_at":stamp(now()),"valid_until":null});
            eprintln!(
                "provisioning operation {} blocked: {}",
                d["operation_id"], result["code"]
            );
        }
        db.execute(
            "UPDATE account_defaults SET document=?1,due=0 WHERE account=?2",
            params![d.to_string(), account],
        )
        .map_err(Error::storage)?;
    }
    let ids: Vec<String> = db.prepare("SELECT id FROM enrollments e WHERE (due=1 OR (json_extract(document,'$.state')='issued' AND verified < ?1)) AND NOT EXISTS(SELECT 1 FROM enrollments successor WHERE successor.predecessor=e.id AND json_extract(successor.document,'$.recovery') IS NOT NULL)")
        .map_err(Error::storage)?.query_map([now()-3],|r| r.get(0)).map_err(Error::storage)?
        .collect::<std::result::Result<_,_>>().map_err(Error::storage)?;
    'enrollments: for id in ids {
        let (account, predecessor): (String, Option<String>) = db
            .query_row(
                "SELECT account,predecessor FROM enrollments WHERE id=?1",
                [&id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .map_err(Error::storage)?;
        let mut d = enrollment(&db, &account, &id)?;
        if let Some(old_id) = predecessor {
            if d["recovery"].is_object() {
                let mapping = adapter(
                    app,
                    "mapping",
                    &json!({"account_id":account,"node_id":d["node_id"],"recovery_safety_check":true}),
                )?;
                if mapping["ok"] != true || mapping["state"] == "active" {
                    d["state"] = json!("uncertain");
                    d["error"] = json!({"code":if mapping["ok"]==true {"recovery_device_rebinding_required"} else {"recovery_mapping_uncertain"},"retryable":false});
                    db.execute("UPDATE enrollments SET document=?1,cipher=NULL,due=0,verified=?2 WHERE id=?3",
                               params![d.to_string(),now(),id]).map_err(Error::storage)?;
                    continue;
                }
            }
            // A later recovery cannot bypass an unresolved earlier creation.
            let ancestors:Vec<String>=db.prepare(
                "WITH RECURSIVE ancestors(id) AS (SELECT ?1 UNION SELECT e.predecessor FROM enrollments e JOIN ancestors a ON e.id=a.id WHERE e.predecessor IS NOT NULL) SELECT id FROM ancestors")
                .map_err(Error::storage)?.query_map([old_id],|r|r.get(0)).map_err(Error::storage)?
                .collect::<std::result::Result<_,_>>().map_err(Error::storage)?;
            for old_id in ancestors {
                if old_id == id {
                    return Err(Error::new(
                        StatusCode::SERVICE_UNAVAILABLE,
                        "invalid_enrollment_ancestry",
                    ));
                }
                let old = enrollment(&db, &account, &old_id)?;
                let result = adapter(app, "revoke", &old)?;
                if result["ok"] != true {
                    d["state"] = json!("uncertain");
                    d["error"] = json!({"code":"revocation_uncertain","retryable":false});
                    db.execute("UPDATE enrollments SET document=?1,cipher=NULL,due=0,verified=?2 WHERE id=?3",
                        params![d.to_string(),now(),id]).map_err(Error::storage)?;
                    continue 'enrollments;
                }
                let mut old = old;
                old["state"] = result["state"].clone();
                old["error"] = Value::Null;
                db.execute(
                    "UPDATE enrollments SET document=?1,cipher=NULL,due=0 WHERE id=?2",
                    params![old.to_string(), old_id],
                )
                .map_err(Error::storage)?;
            }
        }
        // Revalidate the control plane on every issuance/replay job.
        let mut default = document(&db, &account)?;
        let readiness = adapter(app, "ensure", &default)?;
        if readiness["ok"] != true {
            default["state"] = json!("degraded");
            default["error"] = json!({"code":readiness["code"],"retryable":true});
            db.execute(
                "UPDATE account_defaults SET document=?1,due=1 WHERE account=?2",
                params![default.to_string(), account],
            )
            .map_err(Error::storage)?;
            continue;
        }
        default["state"] = json!("ready");
        default["error"] = Value::Null;
        default["valid_until_epoch"] = json!(now() + 60);
        default["readiness"] = json!({"checked_at":stamp(now()),"valid_until":stamp(now()+60)});
        db.execute(
            "UPDATE account_defaults SET document=?1 WHERE account=?2",
            params![default.to_string(), account],
        )
        .map_err(Error::storage)?;
        let stored_cipher: Option<String> = db
            .query_row("SELECT cipher FROM enrollments WHERE id=?1", [&id], |r| {
                r.get(0)
            })
            .map_err(Error::storage)?;
        if let Some(cipher) = stored_cipher {
            match app.decrypt(&cipher, &d) {
                Ok(key) => d["receipt_key"] = json!(key),
                Err(_) => {
                    d["state"] = json!("uncertain");
                    d["error"] = json!({"code":"receipt_recovery_unavailable","retryable":false});
                    db.execute(
                        "UPDATE enrollments SET document=?1,due=0,verified=?2 WHERE id=?3",
                        params![d.to_string(), now(), id],
                    )
                    .map_err(Error::storage)?;
                    continue;
                }
            }
        }
        let result = adapter(app, "enrollment", &d)?;
        // The adapter persists the create-before-command fence before touching Headscale.
        // Retain it when updating the encrypted receipt after the command returns.
        let mut receipt_db = app.db()?;
        let receipt_tx = receipt_db
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(Error::storage)?;
        d = enrollment(&receipt_tx, &account, &id)?;
        let fenced: bool = receipt_tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM enrollments WHERE predecessor=?1 AND json_extract(document,'$.recovery') IS NOT NULL)",
            [&id],|r|r.get(0)).map_err(Error::storage)?;
        if fenced {
            receipt_tx.commit().map_err(Error::storage)?;
            continue;
        }
        d["state"] = result["state"].clone();
        d["error"] = result["error"].clone();
        let cipher = if result["state"] == "issued" {
            d["expires_at"] = result["expires_at"].clone();
            d["headscale_key_id"] = result["key_id"].clone();
            let key = result["auth_key"].as_str().ok_or_else(|| {
                Error::new(StatusCode::SERVICE_UNAVAILABLE, "invalid_worker_receipt")
            })?;
            Some(app.encrypt(key, &d)?)
        } else {
            None
        };
        receipt_tx
            .execute(
                "UPDATE enrollments SET document=?1,cipher=?2,due=0,verified=?3 WHERE id=?4",
                params![d.to_string(), cipher, now(), id],
            )
            .map_err(Error::storage)?;
        receipt_tx.commit().map_err(Error::storage)?;
    }
    let mappings: Vec<(String, String, String)> = db
        .prepare("SELECT account,node,document FROM node_mappings WHERE verified < ?1")
        .map_err(Error::storage)?
        .query_map([now() - 2], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
        .map_err(Error::storage)?
        .collect::<std::result::Result<_, _>>()
        .map_err(Error::storage)?;
    for (account, node, _text) in mappings {
        let input = json!({"account_id":account,"node_id":node});
        let result = adapter(app, "mapping", &input)?;
        let mut mapping_db = app.db()?;
        let mapping_tx = mapping_db
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(Error::storage)?;
        let text: String = mapping_tx
            .query_row(
                "SELECT document FROM node_mappings WHERE account=?1 AND node=?2",
                params![account, node],
                |r| r.get(0),
            )
            .map_err(Error::storage)?;
        let old: Value = serde_json::from_str(&text).map_err(Error::storage)?;
        let pending_recovery: bool=mapping_tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM enrollments WHERE account=?1 AND node=?2 AND json_extract(document,'$.recovery') IS NOT NULL AND json_extract(document,'$.state')!='consumed')",
            params![account,node],|r|r.get(0)).map_err(Error::storage)?;
        let mut result = result;
        if pending_recovery {
            result = json!({"state":"uncertain","device":null,"enrollment_id":null,"setup_intent_id":null,
                          "error":{"code":"node_mapping_pending","retryable":true}});
        }
        let mut d = old.clone();
        d["state"] = result["state"].clone();
        d["device"] = result["device"].clone();
        d["enrollment_id"] = result["enrollment_id"].clone();
        d["setup_intent_id"] = result["setup_intent_id"].clone();
        d["error"] = result["error"].clone();
        if ["state", "device", "enrollment_id", "setup_intent_id"]
            .iter()
            .any(|field| d[*field] != old[*field])
        {
            d["revision"] = json!(old["revision"].as_u64().unwrap_or(0) + 1);
        }
        d["checked_at"] = stamp(now());
        d["valid_until"] = stamp(now() + 5);
        mapping_tx
            .execute(
                "UPDATE node_mappings SET document=?1,verified=?2 WHERE account=?3 AND node=?4",
                params![d.to_string(), now(), account, node],
            )
            .map_err(Error::storage)?;
        mapping_tx.commit().map_err(Error::storage)?;
    }
    Ok(())
}
pub async fn run() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.len() != 2 || !["api", "worker"].contains(&args[0].as_str()) {
        return Err(Error::new(
            StatusCode::BAD_REQUEST,
            "usage_api_or_worker_settings_json",
        ));
    }
    let settings: Settings =
        serde_json::from_slice(&secure_file(FsPath::new(&args[1]))?).map_err(Error::storage)?;
    let app = App::load(settings)?;
    if args[0] == "api" {
        let listener = tokio::net::TcpListener::bind(app.settings.listen)
            .await
            .map_err(Error::storage)?;
        axum::serve(listener, router(app))
            .await
            .map_err(Error::storage)
    } else {
        let path = app.settings.ledger.with_extension("worker.lock");
        let file = private_lock(&path)?;
        file.try_lock_exclusive()
            .map_err(|_| Error::new(StatusCode::CONFLICT, "worker_already_running"))?;
        loop {
            worker_tick(&app)?;
            thread::sleep(Duration::from_millis(250));
        }
    }
}
fn private_lock(path: &FsPath) -> Result<File> {
    use std::os::unix::fs::OpenOptionsExt;
    OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(path)
        .map_err(Error::storage)
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        body::{to_bytes, Body},
        http::Request,
    };
    use jsonwebtoken::{encode, EncodingKey, Header};
    use std::os::unix::fs::PermissionsExt;
    use tower::ServiceExt;

    const ACCOUNT: &str = "11111111-1111-4111-8111-111111111111";
    const NODE: &str = "fossil-0123456789abcdef01234567";
    const INTENT: &str = "33333333-3333-4333-8333-333333333333";
    const PUBLIC: &[u8] = include_bytes!("../tests/fixtures/provisioning-v1/public.pem");
    const PRIVATE: &[u8] =
        include_bytes!("../tests/fixtures/provisioning-v1/private-test-only.pem");

    struct Fixture {
        app: App,
        directory: PathBuf,
    }
    impl Fixture {
        fn new() -> Self {
            let directory =
                std::env::temp_dir().join(format!("swarmsail-private-{}", Uuid::new_v4()));
            fs::create_dir(&directory).unwrap();
            fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
            let ledger = directory.join("ledger.db");
            let secret = directory.join("receipt.key");
            let public = directory.join("public.pem");
            fs::write(&ledger, b"").unwrap();
            fs::write(&secret, [7; 32]).unwrap();
            fs::write(&public, PUBLIC).unwrap();
            for path in [&ledger, &secret] {
                fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
            }
            let app = App::load(Settings {
                ledger,
                issuer: "https://rawrarr.example.test".into(),
                service: "rawrarr-test".into(),
                listen: "127.0.0.1:0".parse().unwrap(),
                public_keys: HashMap::from([("rfc8032-test-only".into(), public)]),
                receipt_key_file: secret,
                tenant_capacity: 10,
                worker: None,
            })
            .unwrap();
            Self { app, directory }
        }
        fn claims(&self) -> Claims {
            Claims {
                iss: self.app.settings.issuer.clone(),
                aud: "swarmsail-provisioning-v1".into(),
                azp: self.app.settings.service.clone(),
                sub: ACCOUNT.into(),
                scope: vec![
                    "tailnet:ensure".into(),
                    "tailnet:read".into(),
                    "node:enroll".into(),
                ],
                iat: now(),
                nbf: now(),
                exp: now() + 300,
                jti: Uuid::new_v4().to_string(),
                node_id: Some(NODE.into()),
                setup_intent_id: Some(INTENT.into()),
                recovery: None,
                recovery_request_id: None,
                recovery_lookup: None,
            }
        }
        fn token(&self, c: &Claims) -> String {
            let mut h = Header::new(Algorithm::EdDSA);
            h.kid = Some("rfc8032-test-only".into());
            encode(&h, c, &EncodingKey::from_ed_pem(PRIVATE).unwrap()).unwrap()
        }
        fn headers(&self) -> HeaderMap {
            let mut h = HeaderMap::new();
            h.insert(
                "authorization",
                format!("Bearer {}", self.token(&self.claims()))
                    .parse()
                    .unwrap(),
            );
            h.insert(
                "idempotency-key",
                Uuid::new_v4().to_string().parse().unwrap(),
            );
            h
        }
        fn ready(&self) {
            let db = self.app.db().unwrap();
            let d = json!({
                "api_version":"1","account_id":ACCOUNT,"tailnet_id":"22222222-2222-4222-8222-222222222222",
                "state":"ready","valid_until_epoch":now()+60,"server_url":"https://22222222-2222-4222-8222-222222222222.swarm.rawrarr.net",
                "readiness":{"checked_at":stamp(now()),"valid_until":stamp(now()+60)}
            });
            db.execute(
                "INSERT INTO account_defaults(account,tailnet,document) VALUES(?1,?2,?3)",
                params![ACCOUNT, d["tailnet_id"].as_str().unwrap(), d.to_string()],
            )
            .unwrap();
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.directory).unwrap();
        }
    }
    #[test]
    fn pinned_auth_rejects_wrong_authority_account_node_and_lifetime() {
        let f = Fixture::new();
        let headers = f.headers();
        assert!(f.app.authorize(&headers, ACCOUNT, "tailnet:ensure").is_ok());
        assert!(f
            .app
            .authorize(
                &headers,
                "22222222-2222-4222-8222-222222222222",
                "tailnet:ensure"
            )
            .is_err());
        let mut c = f.claims();
        assert!(scope_node(&c, NODE, INTENT).is_ok());
        assert!(scope_node(&c, "fossil-aaaaaaaaaaaaaaaaaaaaaaaa", INTENT).is_err());
        for field in ["issuer", "audience", "service", "expiry", "future", "scope"] {
            c = f.claims();
            match field {
                "issuer" => c.iss = "wrong".into(),
                "audience" => c.aud = "wrong".into(),
                "service" => c.azp = "wrong".into(),
                "expiry" => c.exp += 1,
                "future" => {
                    c.iat += 60;
                    c.nbf += 60;
                    c.exp += 60;
                }
                _ => c.scope.clear(),
            }
            let mut h = HeaderMap::new();
            h.insert(
                "authorization",
                format!("Bearer {}", f.token(&c)).parse().unwrap(),
            );
            assert!(
                f.app.authorize(&h, ACCOUNT, "tailnet:ensure").is_err(),
                "{field}"
            );
        }
    }
    #[test]
    fn published_test_vector_signature() {
        let f = Fixture::new();
        let mut c = f.claims();
        c.iat = 1700000000;
        c.nbf = 1700000000;
        c.exp = 1700000300;
        c.jti = "fixture-test-only".into();
        let token = f.token(&c);
        let parts: Vec<_> = token.split('.').collect();
        let fixture: Value = serde_json::from_str(include_str!(
            "../tests/fixtures/provisioning-v1/signature-test.json"
        ))
        .unwrap();
        assert_eq!(parts[1], fixture["payload_segment"].as_str().unwrap());
        assert_eq!(parts[2], fixture["signature_segment"].as_str().unwrap());
        let decoded_header: Value = serde_json::from_slice(
            &base64::engine::general_purpose::URL_SAFE_NO_PAD
                .decode(parts[0])
                .unwrap(),
        )
        .unwrap();
        assert_eq!(decoded_header, fixture["header"]);
        // Signature fixture is expired. Live authorization must still reject it.
        let mut h = HeaderMap::new();
        h.insert("authorization", format!("Bearer {token}").parse().unwrap());
        assert!(f.app.authorize(&h, ACCOUNT, "tailnet:ensure").is_err());
    }
    #[tokio::test]
    async fn ensure_is_default_idempotent_across_distinct_request_keys_and_restart() {
        let f = Fixture::new();
        let first = ensure(
            State(f.app.clone()),
            Path((ACCOUNT.into(), "default-tailnet:ensure".into())),
            f.headers(),
            Ok(Json(Empty {})),
        )
        .await
        .unwrap();
        assert_eq!(first.status(), StatusCode::ACCEPTED);
        for _ in 0..10 {
            ensure(
                State(f.app.clone()),
                Path((ACCOUNT.into(), "default-tailnet:ensure".into())),
                f.headers(),
                Ok(Json(Empty {})),
            )
            .await
            .unwrap();
        }
        let app = App::load(f.app.settings.clone()).unwrap();
        assert_eq!(
            app.db()
                .unwrap()
                .query_row("SELECT COUNT(*) FROM account_defaults", [], |r| r
                    .get::<_, i64>(0))
                .unwrap(),
            1
        );
        assert_eq!(
            document(&app.db().unwrap(), ACCOUNT).unwrap()["state"],
            "queued"
        );
    }
    #[tokio::test]
    async fn route_contract_pending_is_exact_and_operator_routes_are_not_exposed() {
        let f = Fixture::new();
        let token = f.token(&f.claims());
        let app = router(f.app.clone());
        let request = Request::builder()
            .method("POST")
            .uri(format!(
                "/private/v1/accounts/{ACCOUNT}/default-tailnet:ensure"
            ))
            .header("authorization", format!("Bearer {token}"))
            .header("idempotency-key", Uuid::new_v4().to_string())
            .header("content-type", "application/json")
            .body(Body::from("{}"))
            .unwrap();
        let r = app.clone().oneshot(request).await.unwrap();
        assert_eq!(r.status(), StatusCode::ACCEPTED);
        assert_eq!(r.headers()["cache-control"], "no-store");
        let d: Value =
            serde_json::from_slice(&to_bytes(r.into_body(), 8192).await.unwrap()).unwrap();
        assert!(d.get("auth_key").is_none());
        let r = app
            .oneshot(
                Request::builder()
                    .uri("/api/state")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(r.status(), StatusCode::NOT_FOUND);
        let fixture: Value = serde_json::from_str(include_str!(
            "../tests/fixtures/provisioning-v1/enrollment-pending.json"
        ))
        .unwrap();
        let mut internal = fixture.clone();
        internal["expires_at"] = Value::Null;
        internal["server_url"] = json!("reserved");
        internal["reusable"] = json!(false);
        internal["max_uses"] = json!(1);
        internal["account_id"] = json!(ACCOUNT);
        internal["creation_started"] = json!(true);
        assert_eq!(external(internal), fixture);
    }
    #[test]
    fn receipt_is_bound_and_tamper_detected_and_never_in_status() {
        let f = Fixture::new();
        let d = json!({"account_id":ACCOUNT,"node_id":NODE,"setup_intent_id":INTENT,
            "tailnet_id":"tailnet","enrollment_id":"enrollment","headscale_key_id":1,"expires_at":"expiry"});
        let c = f.app.encrypt("test-not-a-live-key", &d).unwrap();
        assert_eq!(f.app.decrypt(&c, &d).unwrap(), "test-not-a-live-key");
        let mut other = d.clone();
        other["node_id"] = json!("other");
        assert!(f.app.decrypt(&c, &other).is_err());
        assert!(f.app.decrypt("bad.receipt", &d).is_err());
    }
    #[test]
    fn new_setup_ids_cannot_bypass_live_key_or_ambiguous_default() {
        let f = Fixture::new();
        f.ready();
        let db = f.app.db().unwrap();
        let req = EnrollmentRequest {
            node_id: NODE.into(),
            setup_intent_id: INTENT.into(),
        };
        let id = create_enrollment(&db, ACCOUNT, &req, None).unwrap();
        let other = EnrollmentRequest {
            node_id: NODE.into(),
            setup_intent_id: Uuid::new_v4().to_string(),
        };
        assert!(create_enrollment(&db, ACCOUNT, &other, None).is_err());
        let successor = create_enrollment(&db, ACCOUNT, &req, Some(&id)).unwrap();
        assert!(create_enrollment(&db, ACCOUNT, &req, Some(&successor)).is_err());
        let mut d = document(&db, ACCOUNT).unwrap();
        d["valid_until_epoch"] = json!(now() - 1);
        db.execute("UPDATE account_defaults SET document=?1", [d.to_string()])
            .unwrap();
        assert_eq!(document(&db, ACCOUNT).unwrap()["state"], "degraded");
    }
    #[test]
    fn idempotency_conflicts_and_unknown_schema_fail_closed() {
        let f = Fixture::new();
        let db = f.app.db().unwrap();
        remember(&db, ACCOUNT, "key", "original", "resource").unwrap();
        assert_eq!(
            previous(&db, ACCOUNT, "key", "original").unwrap(),
            Some("resource".into())
        );
        assert!(previous(&db, ACCOUNT, "key", "different").is_err());
        db.execute("UPDATE provisioning_version SET version=2", [])
            .unwrap();
        assert_eq!(
            App::load(f.app.settings.clone()).err().unwrap().code,
            "ledger_version_unsupported"
        );
    }
    #[tokio::test]
    async fn malformed_body_and_claims_are_versioned_errors_without_secrets() {
        let f = Fixture::new();
        let token = f.token(&f.claims());
        for body in ["not-json", "{\"manual_server_url\":\"https://other.test\"}"] {
            let request = Request::builder()
                .method("POST")
                .uri(format!(
                    "/private/v1/accounts/{ACCOUNT}/default-tailnet:ensure"
                ))
                .header("authorization", format!("Bearer {token}"))
                .header("idempotency-key", Uuid::new_v4().to_string())
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap();
            let r = router(f.app.clone()).oneshot(request).await.unwrap();
            assert_eq!(r.status(), StatusCode::BAD_REQUEST);
            let d: Value =
                serde_json::from_slice(&to_bytes(r.into_body(), 8192).await.unwrap()).unwrap();
            assert_eq!(d["api_version"], "1");
            assert_eq!(d["error"]["code"], "invalid_json");
            assert!(d.get("auth_key").is_none());
        }
    }
    #[test]
    fn concurrent_allocations_keep_one_default_and_persist_authority() {
        let f = Fixture::new();
        let threads: Vec<_> = (0..8)
            .map(|_| {
                let app = f.app.clone();
                let headers = f.headers();
                std::thread::spawn(move || {
                    tokio::runtime::Builder::new_current_thread()
                        .enable_all()
                        .build()
                        .unwrap()
                        .block_on(ensure(
                            State(app),
                            Path((ACCOUNT.into(), "default-tailnet:ensure".into())),
                            headers,
                            Ok(Json(Empty {})),
                        ))
                        .unwrap();
                })
            })
            .collect();
        for t in threads {
            t.join().unwrap();
        }
        assert_eq!(
            f.app
                .db()
                .unwrap()
                .query_row("SELECT COUNT(*) FROM account_defaults", [], |r| r
                    .get::<_, i64>(0))
                .unwrap(),
            1
        );
        let mut settings = f.app.settings.clone();
        settings.issuer = "https://another.example.test".into();
        assert_eq!(
            App::load(settings).err().unwrap().code,
            "authority_binding_conflict"
        );
    }
    #[tokio::test]
    async fn node_metadata_never_returns_stale_address_as_proof() {
        let f = Fixture::new();
        f.ready();
        let db = f.app.db().unwrap();
        create_enrollment(
            &db,
            ACCOUNT,
            &EnrollmentRequest {
                node_id: NODE.into(),
                setup_intent_id: INTENT.into(),
            },
            None,
        )
        .unwrap();
        let mut c = f.claims();
        c.scope.push("node:read".into());
        let mut h = HeaderMap::new();
        h.insert(
            "authorization",
            format!("Bearer {}", f.token(&c)).parse().unwrap(),
        );
        let first = read_node(
            State(f.app.clone()),
            Path((ACCOUNT.into(), NODE.into())),
            h.clone(),
        )
        .await
        .unwrap();
        assert_eq!(first.status(), StatusCode::ACCEPTED);
        let old = json!({"state":"active","device":{"id":"1","ipv4":"100.64.0.1"},"revision":3});
        db.execute(
            "UPDATE node_mappings SET document=?1,verified=?2",
            params![old.to_string(), now() - 10],
        )
        .unwrap();
        let stale = read_node(State(f.app.clone()), Path((ACCOUNT.into(), NODE.into())), h)
            .await
            .unwrap();
        assert_eq!(stale.status(), StatusCode::ACCEPTED);
        let d: Value =
            serde_json::from_slice(&to_bytes(stale.into_body(), 8192).await.unwrap()).unwrap();
        assert_eq!(d["device"], Value::Null);
        assert_eq!(d["checked_at"], Value::Null);
        assert_eq!(d["revision"], 3);
    }
    #[tokio::test]
    async fn recovery_response_fixtures_match_exact_metadata_and_receipt_shapes() {
        let f = Fixture::new();
        f.ready();
        let db = f.app.db().unwrap();
        let issued: Value = serde_json::from_str(include_str!(
            "../tests/fixtures/provisioning-v1/recovery-issued.json"
        ))
        .unwrap();
        let pending: Value = serde_json::from_str(include_str!(
            "../tests/fixtures/provisioning-v1/recovery-pending.json"
        ))
        .unwrap();
        let original = "44444444-4444-4444-8444-444444444444";
        let successor = issued["enrollment_id"].as_str().unwrap();
        let key = Uuid::new_v4().to_string();
        let initial: Value = serde_json::from_str(include_str!(
            "../tests/fixtures/provisioning-v1/recovery-lookup-issued.json"
        ))
        .unwrap();
        db.execute(
            "INSERT INTO enrollments(id,account,node,intent,document) VALUES(?1,?2,?3,?4,?5)",
            params![original, ACCOUNT, NODE, INTENT, initial.to_string()],
        )
        .unwrap();
        remember(
            &db,
            ACCOUNT,
            &key,
            &fingerprint("enroll", &json!({"node_id":NODE,"setup_intent_id":INTENT})),
            original,
        )
        .unwrap();
        let mut claims = f.claims();
        claims.aud = "swarmsail-enrollment-recovery-v1".into();
        claims.scope = vec!["node:recover:lookup".into()];
        claims.recovery_lookup = Some(RecoveryLookup {
            version: "1".into(),
            original_request_id: key.clone(),
            node_id: NODE.into(),
            setup_intent_id: INTENT.into(),
            owner_authorization_id: Uuid::new_v4().to_string(),
        });
        let mut headers = HeaderMap::new();
        headers.insert(
            "authorization",
            format!("Bearer {}", f.token(&claims)).parse().unwrap(),
        );
        let cases: Value = serde_json::from_str(include_str!(
            "../tests/fixtures/provisioning-v1/lookup-metadata-cases.json"
        ))
        .unwrap();
        for (_, expected) in cases.as_object().unwrap() {
            let mut document = expected.clone();
            document
                .as_object_mut()
                .unwrap()
                .remove("successor_enrollment_id");
            db.execute(
                "UPDATE enrollments SET document=?1,cipher='never-decrypt-this',due=0 WHERE id=?2",
                params![document.to_string(), original],
            )
            .unwrap();
            if expected["successor_enrollment_id"].is_string() {
                db.execute("INSERT INTO enrollments(id,account,node,intent,document,predecessor) VALUES(?1,?2,?3,?4,?5,?6)",
                    params![successor,ACCOUNT,NODE,issued["setup_intent_id"].as_str().unwrap(),pending.to_string(),original]).unwrap();
            }
            let response = recovery_lookup(
                State(f.app.clone()),
                Path((ACCOUNT.into(), key.clone())),
                headers.clone(),
            )
            .await
            .unwrap();
            assert_eq!(response.status(), StatusCode::OK);
            let actual: Value =
                serde_json::from_slice(&to_bytes(response.into_body(), 8192).await.unwrap())
                    .unwrap();
            assert_eq!(&actual, expected);
            assert!(actual.get("auth_key").is_none());
            db.execute("DELETE FROM enrollments WHERE id=?1", [successor])
                .unwrap();
        }
        let mut internal = issued.clone();
        internal.as_object_mut().unwrap().remove("auth_key");
        internal["headscale_key_id"] = json!(1);
        internal["delivered"] = json!(false);
        let cipher = f
            .app
            .encrypt(issued["auth_key"].as_str().unwrap(), &internal)
            .unwrap();
        db.execute("INSERT INTO enrollments(id,account,node,intent,document,predecessor,cipher,due,verified) VALUES(?1,?2,?3,?4,?5,?6,?7,0,?8)",
            params![successor,ACCOUNT,NODE,issued["setup_intent_id"].as_str().unwrap(),internal.to_string(),original,cipher,now()]).unwrap();
        for status in [StatusCode::CREATED, StatusCode::OK] {
            let response = receipt(&f.app, ACCOUNT, successor).await.unwrap();
            assert_eq!(response.status(), status);
            let actual: Value =
                serde_json::from_slice(&to_bytes(response.into_body(), 8192).await.unwrap())
                    .unwrap();
            assert_eq!(actual, issued);
        }
        db.execute(
            "UPDATE enrollments SET cipher=NULL WHERE id=?1",
            [successor],
        )
        .unwrap();
        let missing = receipt(&f.app, ACCOUNT, successor)
            .await
            .err()
            .unwrap()
            .into_response();
        let missing: Value =
            serde_json::from_slice(&to_bytes(missing.into_body(), 8192).await.unwrap()).unwrap();
        let expected_missing: Value = serde_json::from_str(include_str!(
            "../tests/fixtures/provisioning-v1/recovery-terminal-cases.json"
        ))
        .unwrap();
        assert_eq!(missing, expected_missing["uncertain"]);
        db.execute("UPDATE enrollments SET verified=0 WHERE id=?1", [successor])
            .unwrap();
        let response = receipt(&f.app, ACCOUNT, successor).await.unwrap();
        assert_eq!(response.status(), StatusCode::ACCEPTED);
        let actual: Value =
            serde_json::from_slice(&to_bytes(response.into_body(), 8192).await.unwrap()).unwrap();
        let mut expected = issued.clone();
        expected.as_object_mut().unwrap().remove("auth_key");
        assert_eq!(actual, expected);
        internal["state"] = json!("issuing");
        internal["expires_at"] = Value::Null;
        db.execute(
            "UPDATE enrollments SET document=?1,due=1,cipher=NULL,verified=0 WHERE id=?2",
            params![internal.to_string(), successor],
        )
        .unwrap();
        let response = receipt(&f.app, ACCOUNT, successor).await.unwrap();
        assert_eq!(response.status(), StatusCode::ACCEPTED);
        let actual: Value =
            serde_json::from_slice(&to_bytes(response.into_body(), 8192).await.unwrap()).unwrap();
        assert_eq!(actual, pending);
        let terminal: Value = serde_json::from_str(include_str!(
            "../tests/fixtures/provisioning-v1/recovery-terminal-cases.json"
        ))
        .unwrap();
        for (state, expected) in terminal.as_object().unwrap() {
            internal["state"] = json!(state);
            db.execute(
                "UPDATE enrollments SET document=?1,due=0,verified=?2 WHERE id=?3",
                params![internal.to_string(), now(), successor],
            )
            .unwrap();
            let response = receipt(&f.app, ACCOUNT, successor)
                .await
                .err()
                .unwrap()
                .into_response();
            assert_eq!(response.status(), StatusCode::CONFLICT);
            let actual: Value =
                serde_json::from_slice(&to_bytes(response.into_body(), 8192).await.unwrap())
                    .unwrap();
            assert_eq!(&actual, expected);
        }
    }
    #[tokio::test]
    async fn recovery_ancestry_cannot_bypass_an_unverified_old_key() {
        let mut f = Fixture::new();
        f.ready();
        let db = f.app.db().unwrap();
        let req = EnrollmentRequest {
            node_id: NODE.into(),
            setup_intent_id: INTENT.into(),
        };
        let first = create_enrollment(&db, ACCOUNT, &req, None).unwrap();
        db.execute("UPDATE enrollments SET document=json_set(document,'$.state','expired'),due=0 WHERE id=?1",[&first]).unwrap();
        let middle = create_enrollment(&db, ACCOUNT, &req, Some(&first)).unwrap();
        db.execute("UPDATE enrollments SET document=json_set(document,'$.state','expired'),due=0 WHERE id=?1",[&middle]).unwrap();
        let last = create_enrollment(&db, ACCOUNT, &req, Some(&middle)).unwrap();
        let scripts = f.directory.join("scripts");
        fs::create_dir(&scripts).unwrap();
        fs::write(scripts.join("__init__.py"), "").unwrap();
        fs::write(
            scripts.join("provisioning_worker.py"),
            format!(
                r#"
import json,sys
d=json.load(sys.stdin)
action=sys.argv[1]
if action=='ensure': print(json.dumps({{'ok':True,'phase':'complete'}}))
elif action=='revoke':
    print(json.dumps({{'ok':d['enrollment_id']!={first:?},'state':'revoked'}}))
else: raise RuntimeError('unsafe descendant must never mint')
"#
            ),
        )
        .unwrap();
        f.app.settings.worker = Some(WorkerSettings {
            repository: f.directory.clone(),
            settings: f.directory.join("test-settings"),
        });
        worker_tick(&f.app).unwrap();
        let d = enrollment(&db, ACCOUNT, &last).unwrap();
        assert_eq!(d["state"], "uncertain");
        assert_eq!(d["error"]["code"], "revocation_uncertain");
        let cipher: Option<String> = db
            .query_row("SELECT cipher FROM enrollments WHERE id=?1", [last], |r| {
                r.get(0)
            })
            .unwrap();
        assert!(cipher.is_none());
    }
    #[tokio::test]
    async fn recovery_lookup_is_metadata_only_and_absence_does_not_fence_delayed_post() {
        let f = Fixture::new();
        f.ready();
        let example: Value = serde_json::from_str(include_str!(
            "../tests/fixtures/provisioning-v1/default-ready.json"
        ))
        .unwrap();
        let mut internal = example.clone();
        internal["valid_until_epoch"] = json!(now() + 60);
        f.app
            .db()
            .unwrap()
            .execute(
                "UPDATE account_defaults SET document=?1",
                [internal.to_string()],
            )
            .unwrap();
        let capability = read_default(State(f.app.clone()), Path(ACCOUNT.into()), f.headers())
            .await
            .unwrap();
        let capability: Value =
            serde_json::from_slice(&to_bytes(capability.into_body(), 8192).await.unwrap()).unwrap();
        assert_eq!(capability, example);
        let key = Uuid::new_v4().to_string();
        let mut c = f.claims();
        c.aud = "swarmsail-enrollment-recovery-v1".into();
        c.scope = vec!["node:recover:lookup".into()];
        c.recovery_lookup = Some(RecoveryLookup {
            version: "1".into(),
            original_request_id: key.clone(),
            node_id: NODE.into(),
            setup_intent_id: INTENT.into(),
            owner_authorization_id: Uuid::new_v4().to_string(),
        });
        let mut h = HeaderMap::new();
        h.insert(
            "authorization",
            format!("Bearer {}", f.token(&c)).parse().unwrap(),
        );
        let missing = recovery_lookup(
            State(f.app.clone()),
            Path((ACCOUNT.into(), key.clone())),
            h.clone(),
        )
        .await
        .err()
        .unwrap()
        .into_response();
        let missing: Value =
            serde_json::from_slice(&to_bytes(missing.into_body(), 8192).await.unwrap()).unwrap();
        let expected: Value = serde_json::from_str(include_str!(
            "../tests/fixtures/provisioning-v1/recovery-lookup-missing.json"
        ))
        .unwrap();
        assert_eq!(missing, expected);
        let mut delayed = f.headers();
        delayed.insert("idempotency-key", key.parse().unwrap());
        let pending = issue(
            State(f.app.clone()),
            Path(ACCOUNT.into()),
            delayed,
            Ok(Json(EnrollmentRequest {
                node_id: NODE.into(),
                setup_intent_id: INTENT.into(),
            })),
        )
        .await
        .unwrap();
        let d: Value =
            serde_json::from_slice(&to_bytes(pending.into_body(), 8192).await.unwrap()).unwrap();
        let id = d["enrollment_id"].as_str().unwrap();
        let restarted = App::load(f.app.settings.clone()).unwrap();
        for state in ["issuing", "issued", "uncertain", "expired"] {
            let db = restarted.db().unwrap();
            let mut d = enrollment(&db, ACCOUNT, id).unwrap();
            if state == "issued" {
                d = serde_json::from_str(include_str!(
                    "../tests/fixtures/provisioning-v1/recovery-lookup-issued.json"
                ))
                .unwrap();
                d["enrollment_id"] = json!(id);
            }
            d["state"] = json!(state);
            db.execute("UPDATE enrollments SET document=?1,cipher='never-decrypted-lookup',due=0 WHERE id=?2",
                       params![d.to_string(),id]).unwrap();
            let r = recovery_lookup(
                State(restarted.clone()),
                Path((ACCOUNT.into(), key.clone())),
                h.clone(),
            )
            .await
            .unwrap();
            let d: Value =
                serde_json::from_slice(&to_bytes(r.into_body(), 8192).await.unwrap()).unwrap();
            assert_eq!(d["enrollment_id"], id);
            assert_eq!(d["state"], state);
            assert!(d.get("auth_key").is_none());
            if state == "issued" {
                let mut expected: Value = serde_json::from_str(include_str!(
                    "../tests/fixtures/provisioning-v1/recovery-lookup-issued.json"
                ))
                .unwrap();
                expected["enrollment_id"] = json!(id);
                assert_eq!(d, expected);
            }
        }
        c.recovery_lookup.as_mut().unwrap().setup_intent_id = Uuid::new_v4().to_string();
        c.setup_intent_id = Some(c.recovery_lookup.as_ref().unwrap().setup_intent_id.clone());
        h.insert(
            "authorization",
            format!("Bearer {}", f.token(&c)).parse().unwrap(),
        );
        let conflict = recovery_lookup(State(restarted), Path((ACCOUNT.into(), key)), h)
            .await
            .err()
            .unwrap()
            .into_response();
        let conflict: Value =
            serde_json::from_slice(&to_bytes(conflict.into_body(), 8192).await.unwrap()).unwrap();
        let expected: Value = serde_json::from_str(include_str!(
            "../tests/fixtures/provisioning-v1/recovery-binding-conflict.json"
        ))
        .unwrap();
        assert_eq!(conflict, expected);
    }
    #[tokio::test]
    async fn recovery_domains_bindings_and_unique_fence_survive_restart() {
        let f = Fixture::new();
        f.ready();
        let db = f.app.db().unwrap();
        let old = create_enrollment(
            &db,
            ACCOUNT,
            &EnrollmentRequest {
                node_id: NODE.into(),
                setup_intent_id: INTENT.into(),
            },
            None,
        )
        .unwrap();
        let binding = RecoveryBinding {
            version: "1".into(),
            predecessor_enrollment_id: old.clone(),
            predecessor_setup_intent_id: INTENT.into(),
            setup_intent_id: Uuid::new_v4().to_string(),
            node_id: NODE.into(),
            owner_authorization_id: Uuid::new_v4().to_string(),
            setup_evidence_sha256: "a".repeat(64),
            account_root_fingerprint: "b".repeat(64),
            role: "metadata".into(),
            capability_digest: "c".repeat(64),
        };
        db.execute("UPDATE enrollments SET cipher='fenced-secret',document=json_set(document,'$.state','issued') WHERE id=?1",[&old]).unwrap();
        db.execute(
            "INSERT INTO node_mappings(account,node,document,verified) VALUES(?1,?2,?3,?4)",
            params![
                ACCOUNT,
                NODE,
                json!({"state":"active","device":{"id":"1"},"revision":2}).to_string(),
                now()
            ],
        )
        .unwrap();
        let make_headers = |b: &RecoveryBinding, key: &str| {
            let mut c = f.claims();
            c.aud = "swarmsail-enrollment-recovery-v1".into();
            c.scope = vec!["node:recover".into()];
            c.setup_intent_id = Some(b.setup_intent_id.clone());
            c.recovery = Some(b.clone());
            c.recovery_request_id = Some(key.into());
            let mut h = HeaderMap::new();
            h.insert(
                "authorization",
                format!("Bearer {}", f.token(&c)).parse().unwrap(),
            );
            h.insert("idempotency-key", key.parse().unwrap());
            h
        };
        let key = Uuid::new_v4().to_string();
        let h = make_headers(&binding, &key);
        assert_eq!(
            recover(
                State(f.app.clone()),
                Path((
                    "99999999-9999-4999-8999-999999999999".into(),
                    format!("{old}:recover")
                )),
                h.clone(),
                Ok(Json(binding.clone()))
            )
            .await
            .err()
            .unwrap()
            .code,
            "account_or_scope_mismatch"
        );
        let mut wrong_setup = binding.clone();
        wrong_setup.predecessor_setup_intent_id = Uuid::new_v4().to_string();
        assert_eq!(
            recover(
                State(f.app.clone()),
                Path((ACCOUNT.into(), format!("{old}:recover"))),
                make_headers(&wrong_setup, &Uuid::new_v4().to_string()),
                Ok(Json(wrong_setup))
            )
            .await
            .err()
            .unwrap()
            .code,
            "recovery_binding_conflict"
        );
        let wrong = recover(
            State(f.app.clone()),
            Path((ACCOUNT.into(), format!("{old}:recover"))),
            f.headers(),
            Ok(Json(binding.clone())),
        )
        .await
        .err()
        .unwrap();
        assert_eq!(wrong.code, "invalid_assertion");
        let mut mismatch = binding.clone();
        mismatch.owner_authorization_id = Uuid::new_v4().to_string();
        assert_eq!(
            recover(
                State(f.app.clone()),
                Path((ACCOUNT.into(), format!("{old}:recover"))),
                h.clone(),
                Ok(Json(mismatch))
            )
            .await
            .err()
            .unwrap()
            .code,
            "recovery_claim_mismatch"
        );
        let restarted = App::load(f.app.settings.clone()).unwrap();
        let mut successor = None;
        let header_a = make_headers(&binding, &Uuid::new_v4().to_string());
        let header_b = make_headers(&binding, &Uuid::new_v4().to_string());
        let (a, b) = tokio::join!(
            recover(
                State(restarted.clone()),
                Path((ACCOUNT.into(), format!("{old}:recover"))),
                header_a,
                Ok(Json(binding.clone()))
            ),
            recover(
                State(restarted.clone()),
                Path((ACCOUNT.into(), format!("{old}:recover"))),
                header_b,
                Ok(Json(binding.clone()))
            )
        );
        let a: Value =
            serde_json::from_slice(&to_bytes(a.unwrap().into_body(), 8192).await.unwrap()).unwrap();
        let b: Value =
            serde_json::from_slice(&to_bytes(b.unwrap().into_body(), 8192).await.unwrap()).unwrap();
        assert_eq!(a["enrollment_id"], b["enrollment_id"]);
        let cipher: Option<String> = db
            .query_row("SELECT cipher FROM enrollments WHERE id=?1", [&old], |r| {
                r.get(0)
            })
            .unwrap();
        assert!(cipher.is_none());
        let mapping: String = db
            .query_row(
                "SELECT document FROM node_mappings WHERE account=?1 AND node=?2",
                params![ACCOUNT, NODE],
                |r| r.get(0),
            )
            .unwrap();
        let mapping: Value = serde_json::from_str(&mapping).unwrap();
        assert_eq!(mapping["state"], "uncertain");
        assert_eq!(mapping["revision"], 3);
        assert!(mapping["device"].is_null());
        let reservation_key = a["recovery_request_id"].clone();
        for header in [
            h.clone(),
            h,
            make_headers(&binding, &Uuid::new_v4().to_string()),
        ] {
            let r = recover(
                State(restarted.clone()),
                Path((ACCOUNT.into(), format!("{old}:recover"))),
                header,
                Ok(Json(binding.clone())),
            )
            .await
            .unwrap();
            assert_eq!(r.status(), StatusCode::ACCEPTED);
            let d: Value =
                serde_json::from_slice(&to_bytes(r.into_body(), 8192).await.unwrap()).unwrap();
            assert_eq!(d["recovery"], serde_json::to_value(&binding).unwrap());
            assert_eq!(d["recovery_request_id"], reservation_key);
            if let Some(ref previous) = successor {
                assert_eq!(&d["enrollment_id"], previous);
            }
            successor = Some(d["enrollment_id"].clone());
        }
        let fenced = receipt(&restarted, ACCOUNT, &old).await.err().unwrap();
        assert_eq!(fenced.code, "enrollment_superseded");
        assert_eq!(
            fenced.successor_enrollment_id.as_deref(),
            successor.as_ref().and_then(Value::as_str)
        );
        let mut other = binding.clone();
        other.setup_intent_id = Uuid::new_v4().to_string();
        assert_eq!(
            recover(
                State(restarted.clone()),
                Path((ACCOUNT.into(), format!("{old}:recover"))),
                make_headers(&other, &Uuid::new_v4().to_string()),
                Ok(Json(other))
            )
            .await
            .err()
            .unwrap()
            .code,
            "recovery_binding_conflict"
        );
        assert_eq!(
            replace(
                State(restarted),
                Path((ACCOUNT.into(), format!("{old}:replace"))),
                f.headers(),
                Ok(Json(Empty {}))
            )
            .await
            .err()
            .unwrap()
            .code,
            "recovery_binding_conflict"
        );
        assert_eq!(
            create_enrollment(
                &db,
                ACCOUNT,
                &EnrollmentRequest {
                    node_id: NODE.into(),
                    setup_intent_id: Uuid::new_v4().to_string(),
                },
                None
            )
            .err()
            .unwrap()
            .code,
            "node_enrollment_exists"
        );
        assert_eq!(
            db.query_row(
                "SELECT COUNT(*) FROM enrollments WHERE predecessor=?1",
                [&old],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
            1
        );
    }
    #[tokio::test]
    async fn terminal_receipts_are_errors_and_replacement_reconciles_one_successor() {
        let f = Fixture::new();
        f.ready();
        let db = f.app.db().unwrap();
        let id = create_enrollment(
            &db,
            ACCOUNT,
            &EnrollmentRequest {
                node_id: NODE.into(),
                setup_intent_id: INTENT.into(),
            },
            None,
        )
        .unwrap();
        for state in ["uncertain", "consumed", "expired", "revoked"] {
            let mut d = enrollment(&db, ACCOUNT, &id).unwrap();
            d["state"] = json!(state);
            db.execute(
                "UPDATE enrollments SET document=?1,due=0,verified=?2 WHERE id=?3",
                params![d.to_string(), now(), id],
            )
            .unwrap();
            let error = receipt(&f.app, ACCOUNT, &id).await.err().unwrap();
            assert_eq!(error.status, StatusCode::CONFLICT);
            assert_eq!(error.code, format!("enrollment_{state}"));
            assert_eq!(error.enrollment_id.as_deref(), Some(id.as_str()));
            let response = error.into_response();
            let error_body: Value =
                serde_json::from_slice(&to_bytes(response.into_body(), 8192).await.unwrap())
                    .unwrap();
            assert_eq!(error_body["enrollment_id"], id);
            assert!(error_body.get("auth_key").is_none());
            let metadata = read_enrollment(
                State(f.app.clone()),
                Path((ACCOUNT.into(), id.clone())),
                f.headers(),
            )
            .await
            .unwrap();
            assert_eq!(metadata.status(), StatusCode::OK);
            let metadata: Value =
                serde_json::from_slice(&to_bytes(metadata.into_body(), 8192).await.unwrap())
                    .unwrap();
            assert_eq!(metadata["state"], state);
            assert!(metadata.get("auth_key").is_none());
        }
        let headers = f.headers();
        let mut successor = None;
        for h in [headers.clone(), headers, f.headers()] {
            let pending = replace(
                State(f.app.clone()),
                Path((ACCOUNT.into(), format!("{id}:replace"))),
                h,
                Ok(Json(Empty {})),
            )
            .await
            .unwrap();
            assert_eq!(pending.status(), StatusCode::ACCEPTED);
            let d: Value =
                serde_json::from_slice(&to_bytes(pending.into_body(), 8192).await.unwrap())
                    .unwrap();
            assert!(d.get("auth_key").is_none());
            if let Some(previous) = &successor {
                assert_eq!(&d["enrollment_id"], previous);
            } else {
                successor = Some(d["enrollment_id"].clone());
            }
        }
        assert_eq!(
            db.query_row(
                "SELECT COUNT(*) FROM enrollments WHERE predecessor=?1",
                [&id],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
            1
        );
    }
    #[tokio::test]
    async fn worker_receipt_is_encrypted_replayed_and_consumed_fail_closed() {
        let mut f = Fixture::new();
        f.ready();
        let scripts = f.directory.join("scripts");
        fs::create_dir(&scripts).unwrap();
        fs::write(scripts.join("__init__.py"), "").unwrap();
        fs::write(scripts.join("provisioning_worker.py"),r#"
import json,sys,datetime
d=json.load(sys.stdin)
if sys.argv[1]=='ensure':
    print(json.dumps({'ok':True,'phase':'complete'}))
else:
    state=d.get('test_state','issued')
    r={'state':state,'error':None}
    if state=='issued':
        r.update(auth_key='public-test-key-never-a-live-credential',key_id=1,
                 expires_at=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(minutes=15)).isoformat())
    print(json.dumps(r))
"#).unwrap();
        f.app.settings.worker = Some(WorkerSettings {
            repository: f.directory.clone(),
            settings: f.directory.join("unused-test-settings"),
        });
        let db = f.app.db().unwrap();
        let id = create_enrollment(
            &db,
            ACCOUNT,
            &EnrollmentRequest {
                node_id: NODE.into(),
                setup_intent_id: INTENT.into(),
            },
            None,
        )
        .unwrap();
        worker_tick(&f.app).unwrap();
        let cipher: String = db
            .query_row("SELECT cipher FROM enrollments WHERE id=?1", [&id], |r| {
                r.get(0)
            })
            .unwrap();
        assert!(!cipher.contains("public-test-key"));
        // Simulate delivery loss: durable original receipt must survive a process restart.
        let restarted = App::load(f.app.settings.clone()).unwrap();
        let first = receipt(&restarted, ACCOUNT, &id).await.unwrap();
        assert_eq!(first.status(), StatusCode::CREATED);
        let first: Value =
            serde_json::from_slice(&to_bytes(first.into_body(), 8192).await.unwrap()).unwrap();
        let second = receipt(&restarted, ACCOUNT, &id).await.unwrap();
        assert_eq!(second.status(), StatusCode::OK);
        let second: Value =
            serde_json::from_slice(&to_bytes(second.into_body(), 8192).await.unwrap()).unwrap();
        assert_eq!(first["auth_key"], second["auth_key"]);
        let request = EnrollmentRequest {
            node_id: NODE.into(),
            setup_intent_id: INTENT.into(),
        };
        let key = Uuid::new_v4().to_string();
        remember(
            &db,
            ACCOUNT,
            &key,
            &fingerprint("enroll", &serde_json::to_value(&request).unwrap()),
            &id,
        )
        .unwrap();
        let mut headers = f.headers();
        headers.insert("idempotency-key", key.parse().unwrap());
        let replay = issue(
            State(restarted.clone()),
            Path(ACCOUNT.into()),
            headers.clone(),
            Ok(Json(request)),
        )
        .await
        .unwrap();
        assert_eq!(replay.status(), StatusCode::OK);
        let replay: Value =
            serde_json::from_slice(&to_bytes(replay.into_body(), 8192).await.unwrap()).unwrap();
        assert_eq!(replay["auth_key"], first["auth_key"]);
        let mut d = enrollment(&db, ACCOUNT, &id).unwrap();
        d["test_state"] = json!("consumed");
        db.execute(
            "UPDATE enrollments SET document=?1,due=1,verified=0 WHERE id=?2",
            params![d.to_string(), id],
        )
        .unwrap();
        worker_tick(&restarted).unwrap();
        let result = receipt(&restarted, ACCOUNT, &id).await.err().unwrap();
        assert_eq!(result.code, "enrollment_consumed");
        let cipher: Option<String> = db
            .query_row("SELECT cipher FROM enrollments WHERE id=?1", [&id], |r| {
                r.get(0)
            })
            .unwrap();
        assert!(cipher.is_none());
        let terminal = issue(
            State(restarted),
            Path(ACCOUNT.into()),
            headers,
            Ok(Json(EnrollmentRequest {
                node_id: NODE.into(),
                setup_intent_id: INTENT.into(),
            })),
        )
        .await
        .err()
        .unwrap();
        assert_eq!(terminal.code, "enrollment_consumed");
        assert_eq!(terminal.enrollment_id.as_deref(), Some(id.as_str()));
    }
}
