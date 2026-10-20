use axum::{
    body::Body,
    extract::{DefaultBodyLimit, Path, State},
    http::{header, HeaderMap, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    routing::{delete, get, post},
    Json, Router,
};
use chrono::{SecondsFormat, Utc};
use regex::Regex;
use reqwest::Client;
use rusqlite::{params, types::ValueRef, Connection, OptionalExtension, Params};
use serde_json::{json, Map, Value};
use std::{
    collections::{BTreeMap, HashSet},
    env, fs, io,
    net::{SocketAddr, TcpStream},
    path::PathBuf,
    process::{Command, Stdio},
    sync::{Arc, Mutex},
    thread,
    time::Duration,
};
use url::Url;
use wait_timeout::ChildExt;

const DASHBOARD: &str = include_str!("../web/index.html");
const CROWDSEC_PAGE: &str = include_str!("../web/crowdsec.html");
const HEALTH_PAGE: &str = include_str!("../web/health.html");
const PUBLIC_ENROLLMENT: &str = include_str!("../web/public-enroll.html");

const SCHEMA: &str = r#"
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK(length(name) BETWEEN 1 AND 100),
    mode TEXT NOT NULL CHECK(mode IN ('collaborative', 'isolated')),
    headscale_instance TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TRIGGER IF NOT EXISTS organization_no_private_reversion
BEFORE UPDATE OF mode ON organizations
WHEN OLD.mode = 'collaborative' AND NEW.mode = 'isolated'
BEGIN
    SELECT RAISE(ABORT, 'a shared organization cannot become isolated');
END;
CREATE TABLE IF NOT EXISTS subscriber_tailnets (
    id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation >= 1),
    organization_id INTEGER NOT NULL UNIQUE REFERENCES organizations(id) ON DELETE RESTRICT,
    headscale_instance TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_id, generation)
);
CREATE TRIGGER IF NOT EXISTS subscriber_tailnet_identity_immutable
BEFORE UPDATE OF account_id, generation, organization_id, headscale_instance
ON subscriber_tailnets
BEGIN
    SELECT RAISE(ABORT, 'subscriber tailnet identity cannot be changed');
END;
CREATE TRIGGER IF NOT EXISTS subscriber_organization_instance_immutable
BEFORE UPDATE OF headscale_instance ON organizations
WHEN EXISTS(SELECT 1 FROM subscriber_tailnets WHERE organization_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'subscriber organization instance cannot be changed');
END;
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 100),
    headscale_node_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(organization_id, name)
);
CREATE TABLE IF NOT EXISTS connections (
    id INTEGER PRIMARY KEY,
    organization_a_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    organization_b_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned', 'applied', 'error')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(organization_a_id < organization_b_id),
    UNIQUE(organization_a_id, organization_b_id)
);
CREATE TABLE IF NOT EXISTS shares (
    id INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    target_organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    permission TEXT NOT NULL CHECK(permission IN ('connect', 'manage')),
    status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned', 'applied', 'error')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(device_id, target_organization_id)
);
CREATE TABLE IF NOT EXISTS acl_rules (
    id INTEGER PRIMARY KEY,
    source_organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    destination_device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    protocol TEXT NOT NULL CHECK(protocol IN ('any', 'tcp', 'udp', 'icmp')),
    ports TEXT NOT NULL DEFAULT '*',
    status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned', 'applied', 'error')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_organization_id, destination_device_id, protocol, ports)
);
"#;

#[derive(Clone)]
struct AppState {
    store: Store,
    config_path: PathBuf,
    inventory_path: Option<PathBuf>,
    allow_generation: bool,
    inventory_cache: Arc<Mutex<Vec<Value>>>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use uuid::Uuid;

    fn test_store() -> (Store, PathBuf) {
        let path = env::temp_dir().join(format!(
            "swarmsail-rust-test-{}-{}.db",
            std::process::id(),
            Uuid::new_v4()
        ));
        (Store::new(path.clone()).expect("initialize store"), path)
    }

    #[test]
    fn creates_and_lists_collaborative_crews() {
        let (store, path) = test_store();
        let created = store
            .create_organization(&json!({"name":"Northstar"}))
            .expect("create organization");
        assert_eq!(created["mode"], "collaborative");
        let state = store.state(&[]).expect("read state");
        assert_eq!(state["organizations"].as_array().unwrap().len(), 1);
        assert_eq!(state["organizations"][0]["name"], "Northstar");
        let _ = fs::remove_file(path);
    }

    #[test]
    fn prevents_shares_between_isolated_crews() {
        let (store, path) = test_store();
        let owner = store
            .create_organization(&json!({
                "name":"Owner","mode":"isolated","headscale_instance":"1"
            }))
            .expect("create isolated organization");
        let target = store
            .create_organization(&json!({
                "name":"Target","mode":"collaborative"
            }))
            .expect("create target organization");
        let device = store
            .create_device(&json!({
                "organization_id":owner["id"],"name":"device"
            }))
            .expect("create device");
        let error = store
            .create_share(&json!({
                "device_id":device["id"],
                "target_organization_id":target["id"]
            }))
            .expect_err("isolated share must be rejected");
        assert!(error.message.contains("isolated"));
        let _ = fs::remove_file(path);
    }

    #[test]
    fn validates_acl_port_ranges() {
        assert!(valid_ports("*"));
        assert!(valid_ports("22,80,8000-8080"));
        assert!(!valid_ports("0"));
        assert!(!valid_ports("8080-80"));
        assert!(!valid_ports("80,"));
    }

    #[test]
    fn public_instance_omits_subscriber_identity_and_paths() {
        let result = public_instance(&json!({
            "id":4,
            "server_url":"https://mast.example.test",
            "subscriber_account_id":"opaque",
            "config_path":"/private/path"
        }));
        assert!(result.get("subscriber_account_id").is_none());
        assert!(result.get("config_path").is_none());
        assert_eq!(result["id"], 4);
    }

    #[test]
    fn subscriber_tailnet_linking_is_idempotent_and_isolated() {
        let (store, path) = test_store();
        let instances = vec![json!({
            "id":10,
            "management_mode":"managed",
            "subscriber_account_id":"11111111-1111-4111-8111-111111111111",
            "subscriber_generation":1
        })];
        store
            .sync_instances(&instances)
            .expect("seed configured instance");
        let first = store
            .link_subscriber_tailnet(
                "11111111-1111-4111-8111-111111111111",
                &json!(10),
                &instances,
            )
            .expect("link subscriber tailnet");
        let retry = store
            .link_subscriber_tailnet(
                "11111111-1111-4111-8111-111111111111",
                &json!("10"),
                &instances,
            )
            .expect("idempotent retry");
        assert_eq!(first, retry);
        let detail = store
            .organization_state(first["organization_id"].as_i64().unwrap(), &instances)
            .expect("read organization");
        assert_eq!(detail["organization"]["mode"], "isolated");
        assert!(detail["tailnet"].get("subscriber_account_id").is_none());
        let _ = fs::remove_file(path);
    }
}

#[derive(Clone)]
struct Store {
    path: PathBuf,
}

#[derive(Debug)]
struct ApiError {
    status: StatusCode,
    message: String,
}

impl ApiError {
    fn new(status: StatusCode, message: impl Into<String>) -> Self {
        Self {
            status,
            message: message.into(),
        }
    }

    fn bad_request(message: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_REQUEST, message)
    }

    fn not_found(message: impl Into<String>) -> Self {
        Self::new(StatusCode::NOT_FOUND, message)
    }

    fn internal(error: impl std::fmt::Display) -> Self {
        Self::new(StatusCode::INTERNAL_SERVER_ERROR, error.to_string())
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let mut response = (self.status, Json(json!({"error": self.message}))).into_response();
        add_security_headers(response.headers_mut());
        response
    }
}

type ApiResult<T> = Result<T, ApiError>;

fn db_error(error: rusqlite::Error) -> ApiError {
    match &error {
        rusqlite::Error::SqliteFailure(code, _)
            if code.code == rusqlite::ErrorCode::ConstraintViolation =>
        {
            ApiError::new(
                StatusCode::CONFLICT,
                format!("record conflicts with existing data: {error}"),
            )
        }
        _ => ApiError::internal(error),
    }
}

fn add_security_headers(headers: &mut HeaderMap) {
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    headers.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    headers.insert(header::X_FRAME_OPTIONS, HeaderValue::from_static("DENY"));
    headers.insert(
        header::CONTENT_SECURITY_POLICY,
        HeaderValue::from_static(
            "default-src 'self'; style-src 'unsafe-inline' 'self'; script-src 'unsafe-inline' 'self'",
        ),
    );
}

fn html(value: impl Into<String>) -> Response {
    let mut response = Response::new(Body::from(value.into()));
    *response.status_mut() = StatusCode::OK;
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/html; charset=utf-8"),
    );
    add_security_headers(response.headers_mut());
    response
}

fn json_ok(value: Value, status: StatusCode) -> Response {
    let mut response = (status, Json(value)).into_response();
    add_security_headers(response.headers_mut());
    response
}

impl Store {
    fn new(path: PathBuf) -> ApiResult<Self> {
        let store = Self { path };
        let db = store.connect().map_err(db_error)?;
        db.execute_batch(SCHEMA).map_err(db_error)?;
        db.execute("DROP INDEX IF EXISTS one_active_subscriber_tailnet", [])
            .map_err(db_error)?;
        let columns: HashSet<String> = {
            let mut statement = db.prepare("PRAGMA table_info(devices)").map_err(db_error)?;
            let result = statement
                .query_map([], |row| row.get::<_, String>(1))
                .map_err(db_error)?
                .collect::<Result<_, _>>()
                .map_err(db_error)?;
            result
        };
        for (name, definition) in [
            ("source", "TEXT NOT NULL DEFAULT 'manual'"),
            ("user_name", "TEXT"),
            ("ip_addresses", "TEXT NOT NULL DEFAULT '[]'"),
            ("online", "INTEGER"),
            ("last_seen", "TEXT"),
        ] {
            if !columns.contains(name) {
                db.execute_batch(&format!(
                    "ALTER TABLE devices ADD COLUMN {name} {definition}"
                ))
                .map_err(db_error)?;
            }
        }
        Ok(store)
    }

    fn connect(&self) -> rusqlite::Result<Connection> {
        let db = Connection::open(&self.path)?;
        db.busy_timeout(Duration::from_secs(10))?;
        db.execute_batch("PRAGMA foreign_keys = ON;")?;
        Ok(db)
    }

    fn state(&self, instances: &[Value]) -> ApiResult<Value> {
        let db = self.connect().map_err(db_error)?;
        let organizations = query_rows(&db, "SELECT * FROM organizations ORDER BY name", [])?;
        let devices = query_rows(
            &db,
            "SELECT d.*, o.name AS organization_name, o.mode AS organization_mode
             FROM devices d JOIN organizations o ON o.id=d.organization_id
             ORDER BY o.name, d.name",
            [],
        )?;
        let connections = query_rows(
            &db,
            "SELECT c.*, a.name AS organization_a_name, b.name AS organization_b_name
             FROM connections c JOIN organizations a ON a.id=c.organization_a_id
             JOIN organizations b ON b.id=c.organization_b_id
             ORDER BY a.name, b.name",
            [],
        )?;
        let shares = query_rows(
            &db,
            "SELECT s.*, d.name AS device_name, owner.id AS owner_organization_id,
                    owner.name AS owner_organization_name, target.name AS target_organization_name
             FROM shares s JOIN devices d ON d.id=s.device_id
             JOIN organizations owner ON owner.id=d.organization_id
             JOIN organizations target ON target.id=s.target_organization_id
             ORDER BY d.name, target.name",
            [],
        )?;
        let acls = query_rows(
            &db,
            "SELECT acl.*, source.name AS source_organization_name,
                    device.name AS destination_device_name,
                    destination.id AS destination_organization_id,
                    destination.name AS destination_organization_name
             FROM acl_rules acl JOIN organizations source ON source.id=acl.source_organization_id
             JOIN devices device ON device.id=acl.destination_device_id
             JOIN organizations destination ON destination.id=device.organization_id
             ORDER BY source.name, destination.name, device.name",
            [],
        )?;
        let public_instances = instances.iter().map(public_instance).collect::<Vec<_>>();
        Ok(json!({
            "organizations": organizations,
            "devices": devices,
            "connections": connections,
            "shares": shares,
            "acl_rules": acls,
            "headscale_instances": public_instances,
            "sharing_note": "Planned shares describe desired policy. Only collaborative Crews can participate; a future Headscale adapter must apply them."
        }))
    }

    fn organization_state(&self, organization_id: i64, instances: &[Value]) -> ApiResult<Value> {
        let state = self.state(instances)?;
        let organization = state["organizations"]
            .as_array()
            .and_then(|items| {
                items
                    .iter()
                    .find(|item| item["id"].as_i64() == Some(organization_id))
            })
            .cloned()
            .ok_or_else(|| ApiError::not_found("Crew does not exist"))?;
        let instance_id = organization["headscale_instance"].clone();
        let tailnet = instances
            .iter()
            .find(|item| {
                item["id"].to_string().trim_matches('"')
                    == instance_id.to_string().trim_matches('"')
            })
            .map(public_instance)
            .unwrap_or(Value::Null);
        let related = |key: &str, predicate: &dyn Fn(&Value) -> bool| -> Vec<Value> {
            state[key]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|item| predicate(item))
                .cloned()
                .collect()
        };
        Ok(json!({
            "organization": organization,
            "tailnet": tailnet,
            "devices": related("devices", &|item| item["organization_id"].as_i64() == Some(organization_id)),
            "connections": related("connections", &|item| item["organization_a_id"].as_i64() == Some(organization_id) || item["organization_b_id"].as_i64() == Some(organization_id)),
            "shares": related("shares", &|item| item["owner_organization_id"].as_i64() == Some(organization_id) || item["target_organization_id"].as_i64() == Some(organization_id)),
            "acl_rules": related("acl_rules", &|item| item["source_organization_id"].as_i64() == Some(organization_id) || item["destination_organization_id"].as_i64() == Some(organization_id)),
            "default_action": "deny",
            "policy_status": "Desired policy only. Planned rules are not active in Headscale until a policy reconciler applies them."
        }))
    }

    fn sync_instances(&self, instances: &[Value]) -> ApiResult<()> {
        let mut db = self.connect().map_err(db_error)?;
        let transaction = db.transaction().map_err(db_error)?;
        for instance in instances {
            let id = json_id_string(&instance["id"]);
            let name = instance["display_name"]
                .as_str()
                .or_else(|| instance["slug"].as_str())
                .map(str::to_owned)
                .unwrap_or_else(|| id.clone());
            let mode = instance["organization_mode"]
                .as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| {
                    if instance["management_mode"].as_str() == Some("adopted") {
                        "collaborative".to_owned()
                    } else {
                        "isolated".to_owned()
                    }
                });
            transaction
                .execute(
                    "INSERT OR IGNORE INTO organizations(name, mode, headscale_instance) VALUES (?1, ?2, ?3)",
                    params![name, mode, id],
                )
                .map_err(db_error)?;
        }
        transaction.commit().map_err(db_error)
    }

    fn link_subscriber_tailnet(
        &self,
        account_id: &str,
        instance_id: &Value,
        instances: &[Value],
    ) -> ApiResult<Value> {
        let account = account_id
            .parse::<uuid::Uuid>()
            .map_err(|_| ApiError::bad_request("account_id must be a UUID"))?
            .hyphenated()
            .to_string();
        if account != account_id {
            return Err(ApiError::bad_request("account_id must be a canonical UUID"));
        }
        let id = json_id_string(instance_id);
        let instance = instances
            .iter()
            .find(|item| json_id_string(&item["id"]) == id)
            .ok_or_else(|| {
                ApiError::bad_request("account requires a configured managed Headscale instance")
            })?;
        if instance["management_mode"].as_str().unwrap_or("managed") != "managed" {
            return Err(ApiError::bad_request(
                "account requires a configured managed Headscale instance",
            ));
        }
        if instance["subscriber_account_id"].as_str() != Some(account.as_str()) {
            return Err(ApiError::new(
                StatusCode::CONFLICT,
                "Headscale instance is assigned to another account",
            ));
        }
        let generation = instance["subscriber_generation"].as_i64().unwrap_or(1);
        if generation < 1 || instance["subscriber_generation"].is_boolean() {
            return Err(ApiError::bad_request(
                "subscriber generation must be a positive integer",
            ));
        }
        let mut db = self.connect().map_err(db_error)?;
        let tx = db.transaction().map_err(db_error)?;
        let existing = fetch_optional(
            &tx,
            "SELECT * FROM subscriber_tailnets WHERE headscale_instance=?1",
            [&id],
        )?;
        if let Some(existing) = existing {
            if existing["account_id"].as_str() != Some(account.as_str())
                || existing["generation"].as_i64() != Some(generation)
            {
                return Err(ApiError::new(
                    StatusCode::CONFLICT,
                    "Headscale instance is already assigned",
                ));
            }
            tx.commit().map_err(db_error)?;
            return Ok(existing);
        }
        if fetch_optional(
            &tx,
            "SELECT 1 AS present FROM subscriber_tailnets WHERE account_id=?1 AND generation=?2",
            params![account, generation],
        )?
        .is_some()
        {
            return Err(ApiError::new(
                StatusCode::CONFLICT,
                "subscriber generation is already assigned",
            ));
        }
        let organization = fetch_optional(
            &tx,
            "SELECT id,mode FROM organizations WHERE headscale_instance=?1",
            [&id],
        )?;
        let organization_id = if let Some(organization) = organization {
            let organization_id = organization["id"].as_i64().unwrap_or_default();
            let has_devices = fetch_optional(
                &tx,
                "SELECT 1 AS present FROM devices WHERE organization_id=?1",
                [organization_id],
            )?
            .is_some();
            if organization["mode"] != "isolated" || has_devices {
                return Err(ApiError::new(
                    StatusCode::CONFLICT,
                    "Headscale instance is already in use",
                ));
            }
            organization_id
        } else {
            tx.execute(
                "INSERT INTO organizations(name,mode,headscale_instance) VALUES(?1,'isolated',?2)",
                params![format!("rawrarr-tailnet-{id}"), id],
            )
            .map_err(db_error)?;
            tx.last_insert_rowid()
        };
        tx.execute(
            "INSERT INTO subscriber_tailnets(account_id,generation,organization_id,headscale_instance)
             VALUES(?1,?2,?3,?4)",
            params![account, generation, organization_id, id],
        )
        .map_err(db_error)?;
        let linked = fetch_one(
            &tx,
            "SELECT * FROM subscriber_tailnets WHERE id=?1",
            [tx.last_insert_rowid()],
        )?;
        tx.commit().map_err(db_error)?;
        Ok(linked)
    }

    fn sync_node_inventory(&self, inventory: &[Value]) -> ApiResult<()> {
        let mut db = self.connect().map_err(db_error)?;
        let tx = db.transaction().map_err(db_error)?;
        for tailnet in inventory {
            let instance_id = json_id_string(&tailnet["instance_id"]);
            let organization = tx
                .query_row(
                    "SELECT id FROM organizations WHERE headscale_instance=?1",
                    [&instance_id],
                    |row| row.get::<_, i64>(0),
                )
                .optional()
                .map_err(db_error)?;
            let Some(organization_id) = organization else {
                continue;
            };
            for node in tailnet["nodes"].as_array().into_iter().flatten() {
                let node_id = json_id_string(&node["id"]);
                let name = node["given_name"]
                    .as_str()
                    .or_else(|| node["name"].as_str())
                    .map(str::to_owned)
                    .unwrap_or_else(|| format!("node-{node_id}"));
                let user_name = node["user_name"].as_str();
                let ips = node
                    .get("ip_addresses")
                    .cloned()
                    .unwrap_or_else(|| json!([]));
                let online = node["online"].as_bool().unwrap_or(false);
                let last_seen = node["last_seen"].as_str();
                let existing = tx
                    .query_row(
                        "SELECT id FROM devices WHERE organization_id=?1 AND headscale_node_id=?2",
                        params![organization_id, node_id],
                        |row| row.get::<_, i64>(0),
                    )
                    .optional()
                    .map_err(db_error)?;
                if let Some(existing_id) = existing {
                    tx.execute(
                        "UPDATE devices SET name=?1, user_name=?2, ip_addresses=?3, online=?4, last_seen=?5, source='headscale' WHERE id=?6",
                        params![name, user_name, ips.to_string(), online, last_seen, existing_id],
                    )
                    .map_err(db_error)?;
                } else {
                    let duplicate = tx
                        .query_row(
                            "SELECT 1 FROM devices WHERE organization_id=?1 AND name=?2",
                            params![organization_id, name],
                            |_| Ok(()),
                        )
                        .optional()
                        .map_err(db_error)?
                        .is_some();
                    let candidate = if duplicate {
                        format!("{name} #{node_id}")
                    } else {
                        name
                    };
                    tx.execute(
                        "INSERT INTO devices(organization_id,name,headscale_node_id,source,user_name,ip_addresses,online,last_seen)
                         VALUES(?1,?2,?3,'headscale',?4,?5,?6,?7)",
                        params![organization_id, candidate, node_id, user_name, ips.to_string(), online, last_seen],
                    )
                    .map_err(db_error)?;
                }
            }
        }
        tx.commit().map_err(db_error)
    }

    fn create_organization(&self, data: &Value) -> ApiResult<Value> {
        let name = required_text(data, "name")?;
        let mode = data["mode"].as_str().unwrap_or("collaborative");
        if !["collaborative", "isolated"].contains(&mode) {
            return Err(ApiError::bad_request(
                "mode must be collaborative or isolated",
            ));
        }
        let instance = optional_text(data.get("headscale_instance"))?;
        if mode == "isolated" && instance.as_deref().unwrap_or("").is_empty() {
            return Err(ApiError::bad_request("isolated Crews require a Mast"));
        }
        let db = self.connect().map_err(db_error)?;
        db.execute(
            "INSERT INTO organizations(name,mode,headscale_instance) VALUES(?1,?2,?3)",
            params![name, mode, instance],
        )
        .map_err(db_error)?;
        fetch_one(
            &db,
            "SELECT * FROM organizations WHERE id=?1",
            [db.last_insert_rowid()],
        )
    }

    fn create_device(&self, data: &Value) -> ApiResult<Value> {
        let organization_id = required_id(data, "organization_id")?;
        let name = required_text(data, "name")?;
        let node_id = optional_text(data.get("headscale_node_id"))?;
        let db = self.connect().map_err(db_error)?;
        db.execute(
            "INSERT INTO devices(organization_id,name,headscale_node_id) VALUES(?1,?2,?3)",
            params![organization_id, name, node_id],
        )
        .map_err(db_error)?;
        fetch_one(
            &db,
            "SELECT * FROM devices WHERE id=?1",
            [db.last_insert_rowid()],
        )
    }

    fn create_connection(&self, data: &Value) -> ApiResult<Value> {
        let mut first = required_id(data, "organization_a_id")?;
        let mut second = required_id(data, "organization_b_id")?;
        if first == second {
            return Err(ApiError::bad_request("a Crew cannot connect to itself"));
        }
        if first > second {
            std::mem::swap(&mut first, &mut second);
        }
        let db = self.connect().map_err(db_error)?;
        let organizations = query_rows(
            &db,
            "SELECT id,mode FROM organizations WHERE id IN (?1,?2)",
            params![first, second],
        )?;
        if organizations.len() != 2 {
            return Err(ApiError::bad_request("both Crews must exist"));
        }
        if organizations.iter().any(|row| row["mode"] == "isolated") {
            return Err(ApiError::bad_request("isolated Crews cannot be connected"));
        }
        db.execute(
            "INSERT INTO connections(organization_a_id,organization_b_id) VALUES(?1,?2)",
            params![first, second],
        )
        .map_err(db_error)?;
        fetch_one(
            &db,
            "SELECT * FROM connections WHERE id=?1",
            [db.last_insert_rowid()],
        )
    }

    fn create_share(&self, data: &Value) -> ApiResult<Value> {
        let device_id = required_id(data, "device_id")?;
        let target_id = required_id(data, "target_organization_id")?;
        let permission = data["permission"].as_str().unwrap_or("connect");
        if !["connect", "manage"].contains(&permission) {
            return Err(ApiError::bad_request(
                "permission must be connect or manage",
            ));
        }
        let db = self.connect().map_err(db_error)?;
        let device = fetch_optional(
            &db,
            "SELECT d.organization_id, owner.mode AS owner_mode, target.mode AS target_mode
             FROM devices d JOIN organizations owner ON owner.id=d.organization_id
             JOIN organizations target ON target.id=?1 WHERE d.id=?2",
            params![target_id, device_id],
        )?
        .ok_or_else(|| ApiError::bad_request("device or target organization does not exist"))?;
        let owner_id = device["organization_id"].as_i64().unwrap_or_default();
        if owner_id == target_id {
            return Err(ApiError::bad_request(
                "a device is already available to its owning organization",
            ));
        }
        if device["owner_mode"] == "isolated" || device["target_mode"] == "isolated" {
            return Err(ApiError::bad_request(
                "isolated Crews cannot create or receive device shares",
            ));
        }
        let (a, b) = ordered(owner_id, target_id);
        let connected = fetch_optional(
            &db,
            "SELECT 1 AS present FROM connections WHERE organization_a_id=?1 AND organization_b_id=?2",
            params![a, b],
        )?
        .is_some();
        if !connected {
            return Err(ApiError::bad_request(
                "Crews must be connected before devices can be shared",
            ));
        }
        db.execute(
            "INSERT INTO shares(device_id,target_organization_id,permission) VALUES(?1,?2,?3)",
            params![device_id, target_id, permission],
        )
        .map_err(db_error)?;
        fetch_one(
            &db,
            "SELECT * FROM shares WHERE id=?1",
            [db.last_insert_rowid()],
        )
    }

    fn create_acl(&self, data: &Value) -> ApiResult<Value> {
        let source_id = required_id(data, "source_organization_id")?;
        let device_id = required_id(data, "destination_device_id")?;
        let protocol = data["protocol"].as_str().unwrap_or("any");
        if !["any", "tcp", "udp", "icmp"].contains(&protocol) {
            return Err(ApiError::bad_request(
                "protocol must be any, tcp, udp, or icmp",
            ));
        }
        let mut ports = data["ports"].as_str().unwrap_or("*").trim().to_owned();
        if protocol == "icmp" {
            ports = "*".to_owned();
        }
        if !valid_ports(&ports) {
            return Err(ApiError::bad_request(
                "ports must be *, a port, a range, or a comma-separated list",
            ));
        }
        let db = self.connect().map_err(db_error)?;
        let destination = fetch_optional(
            &db,
            "SELECT d.organization_id AS destination_id, source.mode AS source_mode,
                    destination.mode AS destination_mode
             FROM devices d JOIN organizations source ON source.id=?1
             JOIN organizations destination ON destination.id=d.organization_id
             WHERE d.id=?2",
            params![source_id, device_id],
        )?
        .ok_or_else(|| ApiError::bad_request("source Crew or destination device does not exist"))?;
        let destination_id = destination["destination_id"].as_i64().unwrap_or_default();
        if source_id != destination_id {
            if destination["source_mode"] == "isolated"
                || destination["destination_mode"] == "isolated"
            {
                return Err(ApiError::bad_request(
                    "isolated Crews cannot have cross-Crew ACLs",
                ));
            }
            let (a, b) = ordered(source_id, destination_id);
            if fetch_optional(
                &db,
                "SELECT 1 AS present FROM connections WHERE organization_a_id=?1 AND organization_b_id=?2",
                params![a, b],
            )?
            .is_none()
            {
                return Err(ApiError::bad_request(
                    "Crews must be connected before adding an ACL",
                ));
            }
        }
        db.execute(
            "INSERT INTO acl_rules(source_organization_id,destination_device_id,protocol,ports) VALUES(?1,?2,?3,?4)",
            params![source_id, device_id, protocol, ports],
        )
        .map_err(db_error)?;
        fetch_one(
            &db,
            "SELECT * FROM acl_rules WHERE id=?1",
            [db.last_insert_rowid()],
        )
    }

    fn get_device(&self, organization_id: i64, device_id: i64) -> ApiResult<Value> {
        let db = self.connect().map_err(db_error)?;
        fetch_optional(
            &db,
            "SELECT * FROM devices WHERE id=?1 AND organization_id=?2",
            params![device_id, organization_id],
        )?
        .ok_or_else(|| ApiError::not_found("device does not exist in this Crew"))
    }

    fn delete_device(&self, organization_id: i64, device_id: i64) -> ApiResult<()> {
        let db = self.connect().map_err(db_error)?;
        let affected = db
            .execute(
                "DELETE FROM devices WHERE id=?1 AND organization_id=?2",
                params![device_id, organization_id],
            )
            .map_err(db_error)?;
        if affected != 1 {
            return Err(ApiError::not_found("device does not exist in this Crew"));
        }
        Ok(())
    }

    fn delete_headscale_devices(&self, organization_id: i64, user_name: &str) -> ApiResult<usize> {
        let db = self.connect().map_err(db_error)?;
        db.execute(
            "DELETE FROM devices WHERE organization_id=?1 AND source='headscale' AND user_name=?2",
            params![organization_id, user_name],
        )
        .map_err(db_error)
    }

    fn delete(&self, table: &str, id: i64, message: &str) -> ApiResult<()> {
        let sql = match table {
            "shares" => "DELETE FROM shares WHERE id=?1",
            "acl_rules" => "DELETE FROM acl_rules WHERE id=?1",
            _ => return Err(ApiError::internal("invalid delete target")),
        };
        let db = self.connect().map_err(db_error)?;
        if db.execute(sql, [id]).map_err(db_error)? != 1 {
            return Err(ApiError::not_found(message));
        }
        Ok(())
    }
}

fn query_rows<P: Params>(db: &Connection, sql: &str, params: P) -> ApiResult<Vec<Value>> {
    let mut statement = db.prepare(sql).map_err(db_error)?;
    let names = statement
        .column_names()
        .iter()
        .map(|name| (*name).to_owned())
        .collect::<Vec<_>>();
    let mapped = statement
        .query_map(params, |row| {
            let mut object = Map::new();
            for (index, name) in names.iter().enumerate() {
                let value = match row.get_ref(index)? {
                    ValueRef::Null => Value::Null,
                    ValueRef::Integer(value) => json!(value),
                    ValueRef::Real(value) => json!(value),
                    ValueRef::Text(value) => {
                        Value::String(String::from_utf8_lossy(value).into_owned())
                    }
                    ValueRef::Blob(value) => {
                        Value::String(String::from_utf8_lossy(value).into_owned())
                    }
                };
                object.insert(name.clone(), value);
            }
            Ok(Value::Object(object))
        })
        .map_err(db_error)?;
    mapped.collect::<Result<Vec<_>, _>>().map_err(db_error)
}

fn fetch_optional<P: Params>(db: &Connection, sql: &str, params: P) -> ApiResult<Option<Value>> {
    Ok(query_rows(db, sql, params)?.into_iter().next())
}

fn fetch_one<P: Params>(db: &Connection, sql: &str, params: P) -> ApiResult<Value> {
    fetch_optional(db, sql, params)?
        .ok_or_else(|| ApiError::internal("inserted record disappeared"))
}

fn public_instance(instance: &Value) -> Value {
    let mut object = Map::new();
    for key in [
        "id",
        "server_url",
        "ipv4_prefix",
        "base_domain",
        "management_mode",
        "security_path",
        "display_name",
    ] {
        if let Some(value) = instance.get(key) {
            object.insert(key.to_owned(), value.clone());
        }
    }
    Value::Object(object)
}

fn json_id_string(value: &Value) -> String {
    value
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| value.to_string().trim_matches('"').to_owned())
}

fn required_text(data: &Value, key: &str) -> ApiResult<String> {
    let value = data
        .get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty() && value.chars().count() <= 100)
        .ok_or_else(|| {
            ApiError::bad_request(format!("{key} must be between 1 and 100 characters"))
        })?;
    Ok(value.to_owned())
}

fn optional_text(value: Option<&Value>) -> ApiResult<Option<String>> {
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) if value.is_empty() => Ok(None),
        Some(Value::String(value)) if value.trim().chars().count() <= 200 => {
            Ok(Some(value.trim().to_owned()))
        }
        _ => Err(ApiError::bad_request(
            "optional text fields must be at most 200 characters",
        )),
    }
}

fn required_id(data: &Value, key: &str) -> ApiResult<i64> {
    let value = data
        .get(key)
        .ok_or_else(|| ApiError::bad_request(format!("{key} must be an integer")))?;
    let id = if let Some(number) = value.as_i64() {
        number
    } else if let Some(text) = value.as_str() {
        text.parse::<i64>()
            .map_err(|_| ApiError::bad_request(format!("{key} must be an integer")))?
    } else {
        return Err(ApiError::bad_request(format!("{key} must be an integer")));
    };
    if id < 1 {
        return Err(ApiError::bad_request(format!("{key} must be positive")));
    }
    Ok(id)
}

fn valid_ports(value: &str) -> bool {
    if value == "*" {
        return true;
    }
    let item_re = Regex::new(r"^\d+(?:-\d+)?$").expect("static regex");
    value.split(',').all(|item| {
        if !item_re.is_match(item) {
            return false;
        }
        let bounds = item
            .split('-')
            .map(str::parse::<u32>)
            .collect::<Result<Vec<_>, _>>();
        match bounds {
            Ok(bounds) if bounds.iter().all(|port| (1..=65535).contains(port)) => {
                bounds.len() == 1 || bounds[0] <= bounds[1]
            }
            _ => false,
        }
    })
}

fn ordered(first: i64, second: i64) -> (i64, i64) {
    if first < second {
        (first, second)
    } else {
        (second, first)
    }
}

fn config(state: &AppState) -> ApiResult<Value> {
    let contents = fs::read_to_string(&state.config_path).map_err(ApiError::internal)?;
    serde_json::from_str(&contents).map_err(ApiError::internal)
}

fn instances(state: &AppState) -> ApiResult<Vec<Value>> {
    Ok(config(state)?["headscale"]["instances"]
        .as_array()
        .cloned()
        .unwrap_or_default())
}

fn refresh_inventory(state: &AppState) -> ApiResult<Vec<Value>> {
    let inventory = if let Some(path) = &state.inventory_path {
        if path.exists() {
            let contents = fs::read_to_string(path).map_err(ApiError::internal)?;
            serde_json::from_str::<Vec<Value>>(&contents).map_err(ApiError::internal)?
        } else {
            Vec::new()
        }
    } else {
        state
            .inventory_cache
            .lock()
            .map_err(ApiError::internal)?
            .clone()
    };
    state.store.sync_node_inventory(&inventory)?;
    *state.inventory_cache.lock().map_err(ApiError::internal)? = inventory.clone();
    Ok(inventory)
}

fn tailnet_for(inventory: &[Value], instance_id: &Value) -> Option<Value> {
    inventory
        .iter()
        .find(|item| json_id_string(&item["instance_id"]) == json_id_string(instance_id))
        .cloned()
}

fn inventory_users(inventory: &[Value], instance_id: &Value) -> Vec<Value> {
    tailnet_for(inventory, instance_id)
        .and_then(|tailnet| tailnet["users"].as_array().cloned())
        .unwrap_or_default()
}

fn organization(state: &AppState, organization_id: i64) -> ApiResult<Value> {
    let all_instances = instances(state)?;
    let detail = state
        .store
        .organization_state(organization_id, &all_instances)?;
    Ok(detail["organization"].clone())
}

fn run_command(program: &str, args: &[String], timeout: Duration) -> ApiResult<String> {
    let mut child = Command::new(program)
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|_| ApiError::new(StatusCode::BAD_GATEWAY, "local management command failed"))?;
    let mut stdout = child
        .stdout
        .take()
        .ok_or_else(|| ApiError::new(StatusCode::BAD_GATEWAY, "local management command failed"))?;
    let mut stderr = child
        .stderr
        .take()
        .ok_or_else(|| ApiError::new(StatusCode::BAD_GATEWAY, "local management command failed"))?;
    let stdout_reader = thread::spawn(move || {
        let mut output = Vec::new();
        io::Read::read_to_end(&mut stdout, &mut output).map(|_| output)
    });
    let stderr_reader = thread::spawn(move || {
        let mut output = Vec::new();
        io::Read::read_to_end(&mut stderr, &mut output).map(|_| output)
    });
    match child
        .wait_timeout(timeout)
        .map_err(|_| ApiError::new(StatusCode::BAD_GATEWAY, "local management command failed"))?
    {
        Some(status) => {
            let stdout = stdout_reader
                .join()
                .map_err(|_| {
                    ApiError::new(StatusCode::BAD_GATEWAY, "local management command failed")
                })?
                .map_err(|_| {
                    ApiError::new(StatusCode::BAD_GATEWAY, "local management command failed")
                })?;
            let _stderr = stderr_reader
                .join()
                .map_err(|_| {
                    ApiError::new(StatusCode::BAD_GATEWAY, "local management command failed")
                })?
                .map_err(|_| {
                    ApiError::new(StatusCode::BAD_GATEWAY, "local management command failed")
                })?;
            if !status.success() {
                return Err(ApiError::new(
                    StatusCode::BAD_GATEWAY,
                    "local management command failed",
                ));
            }
            String::from_utf8(stdout).map_err(|_| {
                ApiError::new(
                    StatusCode::BAD_GATEWAY,
                    "local command returned invalid text",
                )
            })
        }
        None => {
            let _ = child.kill();
            let _ = child.wait();
            let _ = stdout_reader.join();
            let _ = stderr_reader.join();
            Err(ApiError::new(
                StatusCode::GATEWAY_TIMEOUT,
                "local management command timed out",
            ))
        }
    }
}

fn headscale_command(state: &AppState, org: &Value, args: &[String]) -> ApiResult<String> {
    let instance_id = org
        .get("headscale_instance")
        .ok_or_else(|| ApiError::bad_request("Crew has no dedicated Mast"))?;
    let instance = instances(state)?
        .into_iter()
        .find(|item| json_id_string(&item["id"]) == json_id_string(instance_id))
        .ok_or_else(|| ApiError::bad_request("Crew has no dedicated Mast"))?;
    let default_service = format!("headscale-{}", json_id_string(&instance["id"]));
    let service = instance["service_name"]
        .as_str()
        .unwrap_or(&default_service);
    let service_re = Regex::new(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$").expect("static regex");
    if !service_re.is_match(service) {
        return Err(ApiError::new(
            StatusCode::INTERNAL_SERVER_ERROR,
            "configured Mast service name is invalid",
        ));
    }
    let mut command_args = vec![
        "exec".to_owned(),
        service.to_owned(),
        "headscale".to_owned(),
    ];
    command_args.extend(args.to_owned());
    run_command("docker", &command_args, Duration::from_secs(15)).map_err(|_| {
        ApiError::new(
            StatusCode::BAD_GATEWAY,
            "Mast rejected the management request",
        )
    })
}

fn headscale_records(state: &AppState, org: &Value, args: &[String]) -> ApiResult<Vec<Value>> {
    let mut command_args = args.to_vec();
    command_args.extend(["--output".to_owned(), "json".to_owned()]);
    let output = headscale_command(state, org, &command_args)?;
    let value: Value = serde_json::from_str(&output).map_err(|_| {
        ApiError::new(
            StatusCode::BAD_GATEWAY,
            "Mast returned invalid management data",
        )
    })?;
    match value {
        Value::Null => Ok(Vec::new()),
        Value::Array(items) if items.iter().all(Value::is_object) => Ok(items),
        _ => Err(ApiError::new(
            StatusCode::BAD_GATEWAY,
            "Mast returned invalid management data",
        )),
    }
}

async fn healthz() -> Response {
    json_ok(json!({"status":"ok"}), StatusCode::OK)
}

async fn health() -> Response {
    html(HEALTH_PAGE.replace(
        "__CHECKED_AT__",
        &Utc::now().format("%Y-%m-%d %H:%M:%S UTC").to_string(),
    ))
}

async fn dashboard() -> Response {
    html(DASHBOARD)
}

async fn organization_dashboard(Path(id): Path<String>) -> Response {
    if id.parse::<u64>().is_ok() {
        html(DASHBOARD)
    } else {
        json_ok(json!({"error":"not found"}), StatusCode::NOT_FOUND)
    }
}

async fn crowdsec_page() -> Response {
    html(CROWDSEC_PAGE)
}

async fn public_enrollment_page(State(state): State<AppState>) -> ApiResult<Response> {
    let id = env::var("SWARMSAIL_PUBLIC_ENROLL_INSTANCE").unwrap_or_default();
    let instance = instances(&state)?
        .into_iter()
        .find(|item| json_id_string(&item["id"]) == id)
        .ok_or_else(|| ApiError::not_found("public enrollment is disabled"))?;
    let server_url = html_escape(instance["server_url"].as_str().unwrap_or(""));
    Ok(html(
        PUBLIC_ENROLLMENT.replace("__SERVER_URL__", &server_url),
    ))
}

fn html_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}

async fn state_handler(State(state): State<AppState>) -> ApiResult<Response> {
    let _ = refresh_inventory(&state)?;
    let mut result = state.store.state(&instances(&state)?)?;
    result["cloudflare"] = cloudflare_status();
    result["security"] = security_status(&state)?;
    Ok(json_ok(result, StatusCode::OK))
}

async fn organization_handler(
    State(state): State<AppState>,
    Path(id): Path<i64>,
) -> ApiResult<Response> {
    let inventory = refresh_inventory(&state)?;
    let all_instances = instances(&state)?;
    let mut result = state.store.organization_state(id, &all_instances)?;
    let instance_id = result["organization"]["headscale_instance"].clone();
    let tailnet = tailnet_for(&inventory, &instance_id).unwrap_or(Value::Null);
    result["dns"] = if tailnet["dns"].is_object() {
        tailnet["dns"].clone()
    } else {
        Value::Null
    };
    result["headscale_users"] = Value::Array(inventory_users(&inventory, &instance_id));
    Ok(json_ok(result, StatusCode::OK))
}

async fn create_organization_handler(
    State(state): State<AppState>,
    Json(data): Json<Value>,
) -> ApiResult<Response> {
    Ok(json_ok(
        state.store.create_organization(&data)?,
        StatusCode::CREATED,
    ))
}

async fn create_device_handler(
    State(state): State<AppState>,
    Json(data): Json<Value>,
) -> ApiResult<Response> {
    Ok(json_ok(
        state.store.create_device(&data)?,
        StatusCode::CREATED,
    ))
}

async fn create_connection_handler(
    State(state): State<AppState>,
    Json(data): Json<Value>,
) -> ApiResult<Response> {
    Ok(json_ok(
        state.store.create_connection(&data)?,
        StatusCode::CREATED,
    ))
}

async fn create_share_handler(
    State(state): State<AppState>,
    Json(data): Json<Value>,
) -> ApiResult<Response> {
    Ok(json_ok(
        state.store.create_share(&data)?,
        StatusCode::CREATED,
    ))
}

async fn create_acl_handler(
    State(state): State<AppState>,
    Json(data): Json<Value>,
) -> ApiResult<Response> {
    Ok(json_ok(state.store.create_acl(&data)?, StatusCode::CREATED))
}

async fn delete_share_handler(
    State(state): State<AppState>,
    Path(id): Path<i64>,
) -> ApiResult<Response> {
    state.store.delete("shares", id, "share does not exist")?;
    Ok(json_ok(json!({"deleted":true}), StatusCode::OK))
}

async fn delete_acl_handler(
    State(state): State<AppState>,
    Path(id): Path<i64>,
) -> ApiResult<Response> {
    state
        .store
        .delete("acl_rules", id, "ACL rule does not exist")?;
    Ok(json_ok(json!({"deleted":true}), StatusCode::OK))
}

async fn registration_handler(
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Json(data): Json<Value>,
) -> ApiResult<Response> {
    let org = organization(&state, id)?;
    let instance_id = org
        .get("headscale_instance")
        .ok_or_else(|| ApiError::bad_request("Crew has no dedicated Mast"))?;
    let inventory = refresh_inventory(&state)?;
    let user_id = required_id(&data, "user_id")?;
    let users = inventory_users(&inventory, instance_id);
    if !users
        .iter()
        .any(|user| user["id"].as_i64() == Some(user_id))
    {
        return Err(ApiError::bad_request(
            "selected matey does not belong to this Mast",
        ));
    }
    let instance = instances(&state)?
        .into_iter()
        .find(|item| json_id_string(&item["id"]) == json_id_string(instance_id))
        .ok_or_else(|| ApiError::bad_request("Crew has no dedicated Mast"))?;
    let helper = env::var("SWARMSAIL_REGISTRATION_HELPER")
        .unwrap_or_else(|_| "/opt/swarmsail/current/create_preauthkey.py".to_owned());
    let mut args = Vec::new();
    if env::var("SWARMSAIL_CONTAINER").as_deref() != Ok("true") {
        args.push("sudo".to_owned());
    }
    args.extend([
        helper,
        "--config".to_owned(),
        state.config_path.display().to_string(),
        "--instance".to_owned(),
        json_id_string(instance_id),
        "--user".to_owned(),
        user_id.to_string(),
        "--expiration".to_owned(),
        "1h".to_owned(),
    ]);
    let (program, command_args) = if args.first().map(String::as_str) == Some("sudo") {
        ("sudo", &args[1..])
    } else {
        let first = args.remove(0);
        return registration_with_helper(&first, &args, &instance);
    };
    registration_with_helper(program, command_args, &instance)
}

fn registration_with_helper(
    program: &str,
    args: &[String],
    instance: &Value,
) -> ApiResult<Response> {
    let output = run_command(program, args, Duration::from_secs(15)).map_err(|_| {
        ApiError::new(
            StatusCode::BAD_GATEWAY,
            "Headscale could not create a registration key",
        )
    })?;
    let data: Value = serde_json::from_str(&output).map_err(|_| {
        ApiError::new(
            StatusCode::BAD_GATEWAY,
            "Headscale could not create a registration key",
        )
    })?;
    let key = data["key"].as_str().ok_or_else(|| {
        ApiError::new(
            StatusCode::BAD_GATEWAY,
            "Headscale could not create a registration key",
        )
    })?;
    let server_url = instance["server_url"]
        .as_str()
        .unwrap_or("")
        .trim_end_matches('/');
    Ok(json_ok(
        json!({
            "server_url": server_url,
            "auth_key": key,
            "expires_in": "1 hour",
            "command": format!("tailscale login --login-server={server_url} --auth-key={key}")
        }),
        StatusCode::CREATED,
    ))
}

async fn create_user_handler(
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Json(data): Json<Value>,
) -> ApiResult<Response> {
    let org = organization(&state, id)?;
    let instance_id = org
        .get("headscale_instance")
        .ok_or_else(|| ApiError::bad_request("Crew has no dedicated Mast"))?;
    let instance = instances(&state)?
        .into_iter()
        .find(|item| json_id_string(&item["id"]) == json_id_string(instance_id))
        .ok_or_else(|| ApiError::bad_request("Crew has no dedicated Mast"))?;
    let name = data["name"].as_str().unwrap_or("").trim();
    let helper = env::var("SWARMSAIL_USER_HELPER")
        .unwrap_or_else(|_| "/app/create_headscale_user.py".to_owned());
    let args = vec![
        "--config".to_owned(),
        state.config_path.display().to_string(),
        "--instance".to_owned(),
        json_id_string(instance_id),
        "--name".to_owned(),
        name.to_owned(),
    ];
    let (program, command_args) = if env::var("SWARMSAIL_CONTAINER").as_deref() != Ok("true") {
        (
            "sudo".to_owned(),
            std::iter::once(helper).chain(args).collect::<Vec<_>>(),
        )
    } else {
        (helper, args)
    };
    create_user_with_helper(
        &state,
        &program,
        &command_args,
        &instance,
        StatusCode::CREATED,
    )
}

fn create_user_with_helper(
    state: &AppState,
    program: &str,
    args: &[String],
    instance: &Value,
    status: StatusCode,
) -> ApiResult<Response> {
    let output = run_command(program, args, Duration::from_secs(15)).map_err(|_| {
        ApiError::new(
            StatusCode::BAD_GATEWAY,
            "Headscale could not create the user",
        )
    })?;
    let user: Value = serde_json::from_str(&output).map_err(|_| {
        ApiError::new(
            StatusCode::BAD_GATEWAY,
            "Headscale could not create the user",
        )
    })?;
    let mut cache = state.inventory_cache.lock().map_err(ApiError::internal)?;
    if let Some(tailnet) = cache
        .iter_mut()
        .find(|tailnet| json_id_string(&tailnet["instance_id"]) == json_id_string(&instance["id"]))
    {
        if !tailnet["users"].is_array() {
            tailnet["users"] = json!([]);
        }
        tailnet["users"]
            .as_array_mut()
            .expect("users is an array")
            .push(user.clone());
        write_inventory(state, &cache).map_err(ApiError::internal)?;
    }
    Ok(json_ok(user, status))
}

async fn pending_node_handler(
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Json(data): Json<Value>,
) -> ApiResult<Response> {
    let org = organization(&state, id)?;
    let instance_id = org
        .get("headscale_instance")
        .ok_or_else(|| ApiError::bad_request("Crew has no dedicated Mast"))?;
    let inventory = refresh_inventory(&state)?;
    let user_id = required_id(&data, "user_id")?;
    if !inventory_users(&inventory, instance_id)
        .iter()
        .any(|user| user["id"].as_i64() == Some(user_id))
    {
        return Err(ApiError::bad_request(
            "selected matey does not belong to this Mast",
        ));
    }
    let key = data["key"].as_str().unwrap_or("").trim();
    if key.is_empty() {
        return Err(ApiError::bad_request("pending node key is required"));
    }
    let helper = env::var("SWARMSAIL_NODE_REGISTRATION_HELPER")
        .unwrap_or_else(|_| "/app/register_pending_node.py".to_owned());
    let mut args = vec![
        helper,
        "--config".to_owned(),
        state.config_path.display().to_string(),
        "--instance".to_owned(),
        json_id_string(instance_id),
        "--user".to_owned(),
        user_id.to_string(),
        "--key".to_owned(),
        key.to_owned(),
    ];
    let (program, command_args) = if env::var("SWARMSAIL_CONTAINER").as_deref() != Ok("true") {
        let helper = args.remove(0);
        (
            "sudo".to_owned(),
            std::iter::once(helper).chain(args).collect::<Vec<_>>(),
        )
    } else {
        let helper = args.remove(0);
        (helper, args)
    };
    let output = run_command(&program, &command_args, Duration::from_secs(15)).map_err(|_| {
        ApiError::new(
            StatusCode::BAD_GATEWAY,
            "Headscale could not register that pending node key",
        )
    })?;
    let result = serde_json::from_str(&output).map_err(|_| {
        ApiError::new(
            StatusCode::BAD_GATEWAY,
            "Headscale could not register that pending node key",
        )
    })?;
    Ok(json_ok(result, StatusCode::CREATED))
}

async fn delete_device_handler(
    State(state): State<AppState>,
    Path((organization_id, device_id)): Path<(i64, i64)>,
    Json(mut data): Json<Value>,
) -> ApiResult<Response> {
    let _inventory = refresh_inventory(&state)?;
    let org = organization(&state, organization_id)?;
    data["device_id"] = json!(device_id);
    if data["confirm"] != true {
        return Err(ApiError::bad_request(
            "confirm the device removal before continuing",
        ));
    }
    let device = state.store.get_device(organization_id, device_id)?;
    if data["device_name"].as_str() != device["name"].as_str() {
        return Err(ApiError::new(
            StatusCode::CONFLICT,
            "device inventory changed; refresh the Crew and confirm again",
        ));
    }
    let node_id = device["headscale_node_id"].as_str().unwrap_or("");
    let revoked = !node_id.is_empty();
    if revoked {
        if node_id.parse::<u64>().map(|id| id < 1).unwrap_or(true) {
            return Err(ApiError::new(
                StatusCode::CONFLICT,
                "linked device has an invalid Mast node ID",
            ));
        }
        let nodes = headscale_records(&state, &org, &["nodes".into(), "list".into()])?;
        let node = nodes
            .iter()
            .find(|node| json_id_string(&node["id"]) == node_id)
            .ok_or_else(|| {
                ApiError::new(
                    StatusCode::CONFLICT,
                    "linked device is no longer present on this Mast",
                )
            })?;
        let actual_name = node["given_name"]
            .as_str()
            .or_else(|| node["name"].as_str())
            .map(str::to_owned)
            .unwrap_or_else(|| format!("node-{node_id}"));
        if device["name"].as_str() != Some(actual_name.as_str())
            && device["name"].as_str() != Some(format!("{actual_name} #{node_id}").as_str())
        {
            return Err(ApiError::new(
                StatusCode::CONFLICT,
                "device name changed on the Mast; refresh the Crew and confirm again",
            ));
        }
        headscale_command(
            &state,
            &org,
            &[
                "nodes".into(),
                "delete".into(),
                "--identifier".into(),
                node_id.into(),
                "--force".into(),
            ],
        )?;
    }
    state.store.delete_device(organization_id, device_id)?;
    if revoked {
        update_inventory_after_removal(&state, &org, None, &[node_id.to_owned()])?;
    }
    Ok(json_ok(
        json!({"deleted":true,"revoked":revoked}),
        StatusCode::OK,
    ))
}

async fn delete_user_handler(
    State(state): State<AppState>,
    Path((organization_id, user_id)): Path<(i64, i64)>,
    Json(mut data): Json<Value>,
) -> ApiResult<Response> {
    let _inventory = refresh_inventory(&state)?;
    let org = organization(&state, organization_id)?;
    data["user_id"] = json!(user_id);
    if data["confirm"] != true {
        return Err(ApiError::bad_request(
            "confirm the matey removal before continuing",
        ));
    }
    let users = headscale_records(&state, &org, &["users".into(), "list".into()])?;
    let user = users
        .into_iter()
        .find(|item| json_id_string(&item["id"]) == user_id.to_string())
        .ok_or_else(|| ApiError::not_found("matey does not exist on this Mast"))?;
    let user_name = user["name"]
        .as_str()
        .filter(|name| !name.is_empty())
        .ok_or_else(|| ApiError::not_found("matey does not exist on this Mast"))?
        .to_owned();
    let nodes = headscale_records(&state, &org, &["nodes".into(), "list".into()])?;
    let owned_nodes = nodes
        .iter()
        .filter(|node| json_id_string(&node["user"]["id"]) == user_id.to_string())
        .collect::<Vec<_>>();
    let expected_count = data["device_count"].as_i64();
    if expected_count != Some(owned_nodes.len() as i64)
        || data["user_name"].as_str() != Some(user_name.as_str())
    {
        return Err(ApiError::new(
            StatusCode::CONFLICT,
            "matey or device inventory changed; refresh the Crew and confirm again",
        ));
    }
    headscale_command(
        &state,
        &org,
        &[
            "users".into(),
            "destroy".into(),
            "--identifier".into(),
            user_id.to_string(),
            "--force".into(),
        ],
    )?;
    let _ = state
        .store
        .delete_headscale_devices(organization_id, &user_name)?;
    let node_ids = owned_nodes
        .iter()
        .map(|node| json_id_string(&node["id"]))
        .collect::<Vec<_>>();
    update_inventory_after_removal(&state, &org, Some((user_id, &user_name)), &node_ids)?;
    Ok(json_ok(
        json!({"deleted":true,"devices_removed":owned_nodes.len()}),
        StatusCode::OK,
    ))
}

fn update_inventory_after_removal(
    state: &AppState,
    org: &Value,
    user: Option<(i64, &str)>,
    node_ids: &[String],
) -> ApiResult<()> {
    let instance_id = json_id_string(&org["headscale_instance"]);
    let mut cache = state.inventory_cache.lock().map_err(ApiError::internal)?;
    for tailnet in cache
        .iter_mut()
        .filter(|item| json_id_string(&item["instance_id"]) == instance_id)
    {
        if let Some((user_id, _)) = user {
            tailnet["users"].as_array_mut().map(|users| {
                users.retain(|item| json_id_string(&item["id"]) != user_id.to_string())
            });
        }
        let mut removed = node_ids.iter().cloned().collect::<HashSet<_>>();
        if let Some((_, user_name)) = user {
            for node in tailnet["nodes"].as_array().into_iter().flatten() {
                if node["user_name"].as_str() == Some(user_name) {
                    removed.insert(json_id_string(&node["id"]));
                }
            }
        }
        tailnet["nodes"]
            .as_array_mut()
            .map(|nodes| nodes.retain(|item| !removed.contains(&json_id_string(&item["id"]))));
        write_inventory(state, &cache).map_err(|_| {
            ApiError::internal(
                "Mast removal succeeded, but the local inventory could not be refreshed",
            )
        })?;
        break;
    }
    Ok(())
}

fn write_inventory(state: &AppState, inventory: &[Value]) -> io::Result<()> {
    let Some(path) = &state.inventory_path else {
        return Ok(());
    };
    let temporary = path.with_extension(format!(
        "{}.tmp",
        path.extension()
            .and_then(|value| value.to_str())
            .unwrap_or("json")
    ));
    fs::write(
        &temporary,
        format!("{}\n", serde_json::to_string_pretty(inventory)?),
    )?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&temporary, fs::Permissions::from_mode(0o640))?;
    }
    fs::rename(temporary, path)
}

fn cloudflare_status() -> Value {
    json!({
        "configured": env::var("CLOUDFLARE_API_TOKEN").map(|value| !value.is_empty()).unwrap_or(false),
        "zone": env::var("CLOUDFLARE_ZONE").unwrap_or_else(|_| "nerdhirn.tech".to_owned()),
        "name": env::var("CLOUDFLARE_DNS_NAME").unwrap_or_else(|_| "swarmsail.nerdhirn.tech".to_owned()),
        "target": env::var("CLOUDFLARE_DNS_TARGET").unwrap_or_else(|_| "147.182.215.245".to_owned()),
        "proxied": false,
        "activation_ready": env::var("SWARMSAIL_DNS_ACTIVATION_READY").map(|v| v.eq_ignore_ascii_case("true")).unwrap_or(false)
    })
}

fn security_status(state: &AppState) -> ApiResult<Value> {
    let config = config(state)?;
    let crowdsec = &config["crowdsec"];
    let caddy_enabled = config["caddy"]["enabled"].as_bool().unwrap_or(false);
    Ok(json!({
        "crowdsec_required": crowdsec["required_for_public_hosts"].as_bool().unwrap_or(false),
        "crowdsec_management": if crowdsec["externally_managed"].as_bool().unwrap_or(false) { "external" } else { "swarmsail" },
        "http_path": if caddy_enabled { "Caddy → CrowdSec bouncer → Headscale" } else { "NPM Plus → CrowdSec bouncer + AppSec → Headscale" },
        "stun_note": "STUN uses direct UDP and is not HTTP/AppSec traffic."
    }))
}

async fn cloudflare_request(
    path: &str,
    token: &str,
    method: reqwest::Method,
    body: Option<Value>,
) -> ApiResult<Value> {
    let client = Client::builder()
        .timeout(Duration::from_secs(15))
        .build()
        .map_err(|_| {
            ApiError::new(
                StatusCode::BAD_GATEWAY,
                "Cloudflare API could not be reached",
            )
        })?;
    let mut request = client
        .request(
            method,
            format!("https://api.cloudflare.com/client/v4{path}"),
        )
        .bearer_auth(token)
        .header(header::ACCEPT, "application/json");
    if let Some(body) = body {
        request = request.json(&body);
    }
    let response = request.send().await.map_err(|_| {
        ApiError::new(
            StatusCode::BAD_GATEWAY,
            "Cloudflare API could not be reached",
        )
    })?;
    if !response.status().is_success() {
        return Err(ApiError::new(
            StatusCode::BAD_GATEWAY,
            format!(
                "Cloudflare API rejected the request with HTTP {}",
                response.status().as_u16()
            ),
        ));
    }
    let result: Value = response.json().await.map_err(|_| {
        ApiError::new(
            StatusCode::BAD_GATEWAY,
            "Cloudflare API returned invalid data",
        )
    })?;
    if result["success"] != true {
        let errors = result["errors"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|item| item["message"].as_str())
            .collect::<Vec<_>>()
            .join("; ");
        return Err(ApiError::new(
            StatusCode::BAD_GATEWAY,
            format!(
                "Cloudflare API error: {}",
                if errors.is_empty() {
                    "unknown error"
                } else {
                    &errors
                }
            ),
        ));
    }
    Ok(result["result"].clone())
}

async fn configure_dns_handler() -> ApiResult<Response> {
    let token = env::var("CLOUDFLARE_API_TOKEN")
        .ok()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| {
            ApiError::new(
                StatusCode::SERVICE_UNAVAILABLE,
                "CLOUDFLARE_API_TOKEN is not configured",
            )
        })?;
    let status = cloudflare_status();
    if !status["activation_ready"].as_bool().unwrap_or(false) {
        return Err(ApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "DNS activation is locked until the TLS proxy and CrowdSec path are verified",
        ));
    }
    let zone = status["zone"]
        .as_str()
        .unwrap_or("")
        .trim()
        .trim_end_matches('.')
        .to_ascii_lowercase();
    let name = status["name"]
        .as_str()
        .unwrap_or("")
        .trim()
        .trim_end_matches('.')
        .to_ascii_lowercase();
    let target = status["target"].as_str().unwrap_or("").trim();
    if target.parse::<std::net::Ipv4Addr>().is_err() {
        return Err(ApiError::bad_request(
            "Cloudflare automation currently requires an IPv4 target",
        ));
    }
    if name != zone && !name.ends_with(&format!(".{zone}")) {
        return Err(ApiError::bad_request(
            "CLOUDFLARE_DNS_NAME must belong to CLOUDFLARE_ZONE",
        ));
    }
    let mut zones_url =
        Url::parse("https://api.cloudflare.com/client/v4/zones").map_err(ApiError::internal)?;
    zones_url
        .query_pairs_mut()
        .append_pair("name", &zone)
        .append_pair("status", "active");
    let zones_path = format!("/zones?{}", zones_url.query().unwrap_or_default());
    let zones = cloudflare_request(&zones_path, &token, reqwest::Method::GET, None).await?;
    let zones = zones.as_array().cloned().unwrap_or_default();
    if zones.len() != 1 {
        return Err(ApiError::new(
            StatusCode::BAD_GATEWAY,
            "Cloudflare zone was not found or was ambiguous",
        ));
    }
    let zone_id = zones[0]["id"].as_str().unwrap_or("");
    let mut records_url = Url::parse(&format!(
        "https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records"
    ))
    .map_err(ApiError::internal)?;
    records_url
        .query_pairs_mut()
        .append_pair("type", "A")
        .append_pair("name", &name);
    let records_path = format!(
        "/zones/{zone_id}/dns_records?{}",
        records_url.query().unwrap_or_default()
    );
    let records = cloudflare_request(&records_path, &token, reqwest::Method::GET, None)
        .await?
        .as_array()
        .cloned()
        .unwrap_or_default();
    let body = json!({"type":"A","name":name,"content":target,"ttl":300,"proxied":false});
    let (path, method, action) = if let Some(record) = records.first() {
        (
            format!(
                "/zones/{zone_id}/dns_records/{}",
                record["id"].as_str().unwrap_or("")
            ),
            reqwest::Method::PUT,
            "updated",
        )
    } else {
        (
            format!("/zones/{zone_id}/dns_records"),
            reqwest::Method::POST,
            "created",
        )
    };
    let saved = cloudflare_request(&path, &token, method, Some(body)).await?;
    Ok(json_ok(
        json!({"action":action,"id":saved["id"],"name":name,"target":target,"proxied":false}),
        StatusCode::CREATED,
    ))
}

async fn generate_handler(State(state): State<AppState>) -> ApiResult<Response> {
    if !state.allow_generation {
        return Err(ApiError::new(
            StatusCode::FORBIDDEN,
            "generation is disabled for this deployment",
        ));
    }
    let output = Command::new("python3")
        .arg("/app/manage_headscale.py")
        .arg("generate")
        .arg("--config")
        .arg(&state.config_path)
        .arg("--apply")
        .output()
        .map_err(ApiError::internal)?;
    if !output.status.success() {
        return Err(ApiError::bad_request("configuration generation failed"));
    }
    Ok(json_ok(json!({"generated":true}), StatusCode::CREATED))
}

async fn public_enroll_handler(
    State(state): State<AppState>,
    Json(data): Json<Value>,
) -> ApiResult<Response> {
    let instance_id = env::var("SWARMSAIL_PUBLIC_ENROLL_INSTANCE").unwrap_or_default();
    let all_instances = instances(&state)?;
    let _instance = all_instances
        .iter()
        .find(|item| json_id_string(&item["id"]) == instance_id)
        .ok_or_else(|| ApiError::not_found("public enrollment is disabled"))?;
    let user_name = data["user_name"].as_str().unwrap_or("").trim();
    let inventory = refresh_inventory(&state)?;
    let user = inventory_users(&inventory, &json!(instance_id))
        .into_iter()
        .find(|item| item["name"].as_str() == Some(user_name))
        .ok_or_else(|| ApiError::bad_request("owner does not exist in this tailnet"))?;
    state.store.sync_instances(&all_instances)?;
    let organization = state.store.state(&all_instances)?["organizations"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|item| json_id_string(&item["headscale_instance"]) == instance_id)
        .cloned()
        .ok_or_else(|| ApiError::bad_request("tailnet organization is not configured"))?;
    let mut request = data.clone();
    request["user_id"] = user["id"].clone();
    let org = organization;
    let user_id = required_id(&request, "user_id")?;
    let helper = env::var("SWARMSAIL_NODE_REGISTRATION_HELPER")
        .unwrap_or_else(|_| "/app/register_pending_node.py".to_owned());
    let mut args = vec![
        helper,
        "--config".to_owned(),
        state.config_path.display().to_string(),
        "--instance".to_owned(),
        instance_id,
        "--user".to_owned(),
        user_id.to_string(),
        "--key".to_owned(),
        data["key"].as_str().unwrap_or("").to_owned(),
    ];
    let (program, command_args) = if env::var("SWARMSAIL_CONTAINER").as_deref() != Ok("true") {
        let helper = args.remove(0);
        (
            "sudo".to_owned(),
            std::iter::once(helper).chain(args).collect::<Vec<_>>(),
        )
    } else {
        let helper = args.remove(0);
        (helper, args)
    };
    let output = run_command(&program, &command_args, Duration::from_secs(15)).map_err(|_| {
        ApiError::new(
            StatusCode::BAD_GATEWAY,
            "Headscale could not register that pending node key",
        )
    })?;
    let result = serde_json::from_str(&output).map_err(|_| {
        ApiError::new(
            StatusCode::BAD_GATEWAY,
            "Headscale could not register that pending node key",
        )
    })?;
    let _ = org;
    Ok(json_ok(result, StatusCode::CREATED))
}

async fn crowdsec_handler(State(_state): State<AppState>) -> ApiResult<Response> {
    let container =
        env::var("SWARMSAIL_CROWDSEC_CONTAINER").unwrap_or_else(|_| "crowdsec".to_owned());
    let status = run_command(
        "docker",
        &[
            "inspect".into(),
            "--format".into(),
            "{{.State.Status}}|{{.State.StartedAt}}".into(),
            container.clone(),
        ],
        Duration::from_secs(12),
    )
    .map_err(|_| {
        ApiError::new(
            StatusCode::BAD_GATEWAY,
            "CrowdSec local data is unavailable",
        )
    })?;
    let (running, started_at) = status
        .trim()
        .split_once('|')
        .unwrap_or(("unknown", "unknown"));
    let metrics = run_command(
        "docker",
        &[
            "exec".into(),
            container.clone(),
            "cscli".into(),
            "metrics".into(),
            "-o".into(),
            "json".into(),
        ],
        Duration::from_secs(12),
    )
    .map_err(|_| {
        ApiError::new(
            StatusCode::BAD_GATEWAY,
            "CrowdSec local data is unavailable",
        )
    })?;
    let decisions = run_command(
        "docker",
        &[
            "exec".into(),
            container.clone(),
            "cscli".into(),
            "decisions".into(),
            "list".into(),
            "-o".into(),
            "json".into(),
        ],
        Duration::from_secs(12),
    )
    .map_err(|_| {
        ApiError::new(
            StatusCode::BAD_GATEWAY,
            "CrowdSec local data is unavailable",
        )
    })?;
    let logs = run_command(
        "docker",
        &[
            "logs".into(),
            "--since".into(),
            "24h".into(),
            "--tail".into(),
            "500".into(),
            container.clone(),
        ],
        Duration::from_secs(12),
    )
    .map_err(|_| {
        ApiError::new(
            StatusCode::BAD_GATEWAY,
            "CrowdSec local data is unavailable",
        )
    })?;
    let metrics: Value = serde_json::from_str(&metrics).map_err(|_| {
        ApiError::new(
            StatusCode::BAD_GATEWAY,
            "CrowdSec returned invalid local data",
        )
    })?;
    let decisions: Value = serde_json::from_str(&decisions).map_err(|_| {
        ApiError::new(
            StatusCode::BAD_GATEWAY,
            "CrowdSec returned invalid local data",
        )
    })?;
    let mut acquisitions = Vec::new();
    if let Some(map) = metrics["acquisition"].as_object() {
        for (source, values) in map {
            acquisitions.push(json!({
                "source":source,
                "read":values["reads"].as_i64().unwrap_or(0),
                "parsed":values["parsed"].as_i64().unwrap_or(0),
                "unparsed":values["unparsed"].as_i64().unwrap_or(0),
                "whitelisted":values["whitelisted"].as_i64().unwrap_or(0)
            }));
        }
    }
    let mut alerts = metrics["alerts"]
        .as_object()
        .map(|items| items.iter().map(|(scenario,count)| json!({"scenario":scenario,"count":count.as_i64().unwrap_or(0)})).collect::<Vec<_>>())
        .unwrap_or_default();
    alerts.sort_by_key(|item| -item["count"].as_i64().unwrap_or(0));
    alerts.truncate(12);
    let mut active_decisions = Vec::new();
    for alert in decisions.as_array().into_iter().flatten() {
        for decision in alert["decisions"].as_array().into_iter().flatten() {
            active_decisions.push(json!({
                "scenario":decision["scenario"].as_str().unwrap_or("unknown"),
                "type":decision["type"].as_str().unwrap_or("unknown"),
                "origin":decision["origin"].as_str().unwrap_or("unknown"),
                "duration":decision["duration"].as_str().unwrap_or("unknown"),
                "scope":decision["scope"].as_str().unwrap_or("unknown"),
                "target":mask_address(decision["value"].as_str().unwrap_or(""))
            }));
            if active_decisions.len() == 50 {
                break;
            }
        }
        if active_decisions.len() == 50 {
            break;
        }
    }
    let line_re = Regex::new(r"level=(info|warning|error)").expect("static regex");
    let ip_re = Regex::new(r"(?:\d{1,3}\.){3}\d{1,3}").expect("static regex");
    let msg_re = Regex::new(r#"msg="(.*)"$"#).expect("static regex");
    let mut levels = BTreeMap::from([("info", 0_i64), ("warning", 0_i64), ("error", 0_i64)]);
    let mut issues = Vec::new();
    for line in logs.lines() {
        if let Some(found) = line_re.captures(line).and_then(|capture| capture.get(1)) {
            let level = found.as_str();
            *levels.entry(level).or_default() += 1;
            if level != "info" {
                let raw = msg_re
                    .captures(line)
                    .and_then(|capture| capture.get(1))
                    .map(|match_| match_.as_str())
                    .unwrap_or(line);
                issues.push(json!({
                    "level":level,
                    "message":ip_re.replace_all(raw, |capture: &regex::Captures| mask_address(&capture[0])).chars().rev().take(240).collect::<String>().chars().rev().collect::<String>()
                }));
            }
        }
    }
    issues.reverse();
    issues.truncate(20);
    let appsec = metrics["appsec-engine"]
        .as_object()
        .and_then(|items| items.values().next())
        .cloned()
        .unwrap_or(json!({}));
    let bouncers = &metrics["bouncers"]["npmplus"];
    let mut bouncer_dropped = Map::new();
    if let Some(values) = bouncers.as_object() {
        for (key, value) in values {
            if key != "" {
                bouncer_dropped.insert(key.clone(), value.clone());
            }
        }
    }
    let result = json!({
        "generated_at":Utc::now().to_rfc3339_opts(SecondsFormat::Millis,true),
        "container":{"name":container,"status":running,"started_at":started_at},
        "acquisition":acquisitions,
        "top_alerts":alerts,
        "appsec":{"processed":appsec["processed"].as_i64().unwrap_or(0),"blocked":appsec["blocked"].as_i64().unwrap_or(0)},
        "bouncer":{
            "processed":nested_total(&bouncers[""]),
            "dropped":nested_total(&Value::Object(bouncer_dropped))
        },
        "active_decisions":active_decisions,
        "log_window":{"hours":24,"sampled_lines":levels.values().sum::<i64>(),"levels":levels},
        "recent_issues":issues
    });
    Ok(json_ok(result, StatusCode::OK))
}

fn nested_total(value: &Value) -> i64 {
    match value {
        Value::Object(items) => items.values().map(nested_total).sum(),
        Value::Number(number) => number
            .as_i64()
            .or_else(|| number.as_u64().map(|n| n as i64))
            .unwrap_or(0),
        _ => 0,
    }
}

fn mask_address(value: &str) -> String {
    if let Ok(address) = value.parse::<std::net::IpAddr>() {
        match address {
            std::net::IpAddr::V4(address) => {
                let octets = address.octets();
                format!("{}.{}.{}.0/24", octets[0], octets[1], octets[2])
            }
            std::net::IpAddr::V6(address) => {
                let segments = address.segments();
                format!(
                    "{:x}:{:x}:{:x}:{:x}::/64",
                    segments[0], segments[1], segments[2], segments[3]
                )
            }
        }
    } else {
        "redacted".to_owned()
    }
}

async fn not_found() -> Response {
    json_ok(json!({"error":"not found"}), StatusCode::NOT_FOUND)
}

fn app(state: AppState) -> Router {
    Router::new()
        .route("/", get(dashboard))
        .route("/organizations/:id", get(organization_dashboard))
        .route("/crowdsec", get(crowdsec_page))
        .route("/health", get(health))
        .route("/healthz", get(healthz))
        .route("/public-enroll", get(public_enrollment_page))
        .route("/api/state", get(state_handler))
        .route("/api/crowdsec", get(crowdsec_handler))
        .route("/api/organizations/:id", get(organization_handler))
        .route("/api/organizations", post(create_organization_handler))
        .route("/api/devices", post(create_device_handler))
        .route("/api/connections", post(create_connection_handler))
        .route("/api/shares", post(create_share_handler))
        .route("/api/shares/:id", delete(delete_share_handler))
        .route("/api/acls", post(create_acl_handler))
        .route("/api/acls/:id", delete(delete_acl_handler))
        .route(
            "/api/organizations/:id/registration",
            post(registration_handler),
        )
        .route("/api/organizations/:id/users", post(create_user_handler))
        .route(
            "/api/organizations/:id/pending-node",
            post(pending_node_handler),
        )
        .route(
            "/api/organizations/:organization_id/users/:user_id",
            delete(delete_user_handler),
        )
        .route(
            "/api/organizations/:organization_id/devices/:device_id",
            delete(delete_device_handler),
        )
        .route("/api/dns/cloudflare", post(configure_dns_handler))
        .route("/api/generate", post(generate_handler))
        .route("/api/public-enroll", post(public_enroll_handler))
        .fallback(not_found)
        .layer(DefaultBodyLimit::max(64 * 1024))
        .with_state(state)
}

fn parse_args() -> (String, u16, PathBuf, PathBuf, bool, Option<PathBuf>) {
    let mut host = "127.0.0.1".to_owned();
    let mut port = 8787_u16;
    let mut database = PathBuf::from("swarmsail.db");
    let mut config = PathBuf::from("instances.json");
    let mut allow_generation = false;
    let mut node_inventory = None;
    let mut args = env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--host" => host = args.next().unwrap_or(host),
            "--port" => port = args.next().and_then(|v| v.parse().ok()).unwrap_or(port),
            "--database" => database = args.next().map(PathBuf::from).unwrap_or(database),
            "--config" => config = args.next().map(PathBuf::from).unwrap_or(config),
            "--node-inventory" => node_inventory = args.next().map(PathBuf::from),
            "--allow-generation" => allow_generation = true,
            "healthcheck" => {
                std::process::exit(if healthcheck() { 0 } else { 1 });
            }
            _ => {
                eprintln!("unknown argument: {arg}");
                std::process::exit(2);
            }
        }
    }
    (
        host,
        port,
        database,
        config,
        allow_generation,
        node_inventory,
    )
}

fn healthcheck() -> bool {
    let address =
        env::var("SWARMSAIL_HEALTHCHECK_ADDRESS").unwrap_or_else(|_| "127.0.0.1:8787".to_owned());
    let Ok(mut stream) = TcpStream::connect_timeout(
        &address
            .parse::<SocketAddr>()
            .unwrap_or(SocketAddr::from(([127, 0, 0, 1], 8787))),
        Duration::from_secs(3),
    ) else {
        return false;
    };
    use std::io::{Read, Write};
    if stream
        .write_all(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        .is_err()
    {
        return false;
    }
    let mut response = String::new();
    stream.read_to_string(&mut response).is_ok()
        && response.starts_with("HTTP/1.1 200")
        && response.contains(r#"{"status":"ok"}"#)
}

#[tokio::main]
async fn main() {
    let (host, port, database, config_path, allow_generation, inventory_path) = parse_args();
    let store = match Store::new(database) {
        Ok(store) => store,
        Err(error) => {
            eprintln!("database initialization failed: {}", error.message);
            std::process::exit(1);
        }
    };
    let state = AppState {
        store,
        config_path,
        inventory_path,
        allow_generation,
        inventory_cache: Arc::new(Mutex::new(Vec::new())),
    };
    match instances(&state).and_then(|instances| {
        state.store.sync_instances(&instances)?;
        for instance in &instances {
            if let Some(account_id) = instance["subscriber_account_id"].as_str() {
                state
                    .store
                    .link_subscriber_tailnet(account_id, &instance["id"], &instances)?;
            }
        }
        Ok(instances)
    }) {
        Ok(_) => {}
        Err(error) => {
            eprintln!("configuration initialization failed: {}", error.message);
            std::process::exit(1);
        }
    }
    let address = format!("{host}:{port}");
    let listener = tokio::net::TcpListener::bind(&address)
        .await
        .unwrap_or_else(|error| {
            eprintln!("could not listen on {address}: {error}");
            std::process::exit(1);
        });
    println!("Swarmsail Rust backend listening on {address}");
    if let Err(error) = axum::serve(listener, app(state)).await {
        eprintln!("server failed: {error}");
        std::process::exit(1);
    }
}
