use std::{
	net::{SocketAddr, TcpListener},
	path::{Path, PathBuf},
	sync::Arc,
};

use axum::{Router, extract::connect_info::IntoMakeServiceWithConnectInfo};
use axum_server::{Handle, from_tcp_rustls};
use axum_server_dual_protocol::{
	ServerExt, axum_server::tls_rustls::RustlsConfig, from_tcp_dual_protocol,
};
use futures::{FutureExt, future::BoxFuture};
use tokio::sync::broadcast::error::RecvError;
use tuwunel_core::{Result, Server, debug, err, error, info};

/// The process signal that also reloads the configuration; see
/// `config_reload_signal`.
const RELOAD_SIGNAL: &str = "SIGUSR1";

pub(super) async fn serve<'a>(
	server: &Arc<Server>,
	app: &Router,
	handle: &Handle<SocketAddr>,
	cert: &Path,
	key: &Path,
	dual_protocol: bool,
	listeners: impl Iterator<Item = TcpListener>,
) -> Result<Vec<BoxFuture<'a, Result<(), std::io::Error>>>> {
	info!(
		"Note: It is strongly recommended that you use a reverse proxy instead of running \
		 tuwunel directly with TLS."
	);

	debug!(
		"Using direct TLS. Certificate path {cert:?} and certificate private key path {key:?}"
	);

	let conf = RustlsConfig::from_pem_file(cert, key)
		.await
		.map_err(|e| err!(Config("tls", "Failed to load certificates or key: {e}")))?;

	let app = app
		.clone()
		.into_make_service_with_connect_info::<SocketAddr>();

	let futures = if dual_protocol {
		serve_dual_protocol(&app, &conf, handle, listeners)?
	} else {
		serve_tls(&app, &conf, handle, listeners)?
	};

	// Spawned apart from the acceptors rather than joined with them: the
	// caller waits for every acceptor to finish, and a task that lives until
	// shutdown would keep it waiting after a listener had already failed.
	if server.config.config_reload_signal {
		server.runtime().spawn(reload_on_signal(
			server.clone(),
			conf,
			cert.to_path_buf(),
			key.to_path_buf(),
		));
	}

	Ok(futures)
}

/// Reloads the certificate and key from disk each time the reload signal
/// arrives, so a renewed certificate takes effect without a restart.
///
/// The acceptors share the configuration through `RustlsConfig`, which swaps
/// its inner server configuration atomically; connections already open keep
/// the certificate they negotiated. A failed reload logs and leaves the
/// previous certificate in service. The signal channel holds one message, so
/// a lagged receiver may have missed the reload signal; it reloads rather than
/// risk skipping a renewal. Whether the task runs is decided once at startup
/// from `config_reload_signal`; changing that option at runtime does not
/// start or stop it.
async fn reload_on_signal(server: Arc<Server>, conf: RustlsConfig, cert: PathBuf, key: PathBuf) {
	let mut signaled = server.signal.subscribe();
	while server.is_running() {
		tokio::select! {
			() = server.until_shutdown() => break,
			signal = signaled.recv() => match signal {
				| Ok(RELOAD_SIGNAL) | Err(RecvError::Lagged(_)) => {},
				| Ok(_) => continue,
				| Err(RecvError::Closed) => break,
			},
		}

		match conf.reload_from_pem_file(&cert, &key).await {
			| Ok(()) => info!(?cert, "Reloaded the TLS certificate and key"),
			| Err(e) => error!(
				?cert,
				"Failed to reload the TLS certificate; keeping the previous one: {e}"
			),
		}
	}
}

fn serve_dual_protocol<'a>(
	app: &IntoMakeServiceWithConnectInfo<Router, SocketAddr>,
	conf: &RustlsConfig,
	handle: &Handle<SocketAddr>,
	listeners: impl Iterator<Item = TcpListener>,
) -> Result<Vec<BoxFuture<'a, Result<(), std::io::Error>>>> {
	listeners
		.map(|listener| {
			// Pinned against an upstream default flip: a plain request is
			// answered, never redirected to the https scheme.
			let acceptor = from_tcp_dual_protocol(listener, conf.clone())?
				.set_upgrade(false)
				.handle(handle.clone())
				.serve(app.clone())
				.boxed();

			Ok(acceptor)
		})
		.collect()
}

fn serve_tls<'a>(
	app: &IntoMakeServiceWithConnectInfo<Router, SocketAddr>,
	conf: &RustlsConfig,
	handle: &Handle<SocketAddr>,
	listeners: impl Iterator<Item = TcpListener>,
) -> Result<Vec<BoxFuture<'a, Result<(), std::io::Error>>>> {
	listeners
		.map(|listener| {
			let acceptor = from_tcp_rustls(listener, conf.clone())?
				.handle(handle.clone())
				.serve(app.clone())
				.boxed();

			Ok(acceptor)
		})
		.collect()
}
