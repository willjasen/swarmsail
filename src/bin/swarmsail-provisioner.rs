#[path = "../provisioning.rs"]
mod provisioning;

#[tokio::main]
async fn main() {
    if let Err(error) = provisioning::run().await {
        eprintln!("provisioning stopped: {}", error.code);
        std::process::exit(1);
    }
}
