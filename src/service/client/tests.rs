use ipaddress::IPAddress;
use reqwest::Url;

use super::valid_cidr_range_url;

#[test]
fn url_cidr_check_handles_ipv4_ipv6_and_domains() {
	let denylist = [
		IPAddress::parse("10.0.0.0/8").expect("test denylist range parses"),
		IPAddress::parse("::1/128").expect("test denylist range parses"),
	];

	let ipv4 = Url::parse("http://10.1.2.3/").expect("test URL parses");
	let ipv6 = Url::parse("http://[::1]/").expect("test URL parses");
	let allowed = Url::parse("https://8.8.8.8/").expect("test URL parses");
	let domain = Url::parse("https://example.com/").expect("test URL parses");

	assert!(!valid_cidr_range_url(&denylist, &ipv4));
	assert!(!valid_cidr_range_url(&denylist, &ipv6));
	assert!(valid_cidr_range_url(&denylist, &allowed));
	assert!(valid_cidr_range_url(&denylist, &domain));
}

mod trusted_roots {
	use std::{fs, path::PathBuf};

	use super::super::trusted_roots;

	const ROOT_PEM: &str = "-----BEGIN CERTIFICATE-----
MIIBTDCB/6ADAgECAhQnJpzSXlbo9yqpLdswq9X1xdRdqzAFBgMrZXAwHDEaMBgG
A1UEAwwRdHV3dW5lbCB0ZXN0IHJvb3QwHhcNMjYwOTIwMTY1MTIxWhcNMzYwOTE3
MTY1MTIxWjAcMRowGAYDVQQDDBF0dXd1bmVsIHRlc3Qgcm9vdDAqMAUGAytlcAMh
AA6pjRs8DqJYP/dBlqG3/yPtpuRfLfS6PZiVNHwOw++oo1MwUTAdBgNVHQ4EFgQU
W9xg16H4Dn8ef76EcnNGvaK5+YkwHwYDVR0jBBgwFoAUW9xg16H4Dn8ef76EcnNG
vaK5+YkwDwYDVR0TAQH/BAUwAwEB/zAFBgMrZXADQQAfaCLtRbKCbFdwTt5hGgGJ
LiPCuKFSWC/3ZktKrOxbUEjP+iUJeBwMI2mRkFRrIrmkhOQJMMVPXcHuCudOtHUI
-----END CERTIFICATE-----
";

	fn scratch(name: &str, contents: &str) -> PathBuf {
		let path = std::env::temp_dir()
			.join(format!("tuwunel-trusted-roots-{}-{name}", std::process::id()));
		fs::write(&path, contents).expect("scratch file written");
		path
	}

	#[test]
	fn no_paths_yield_no_roots() {
		assert!(
			trusted_roots(&[])
				.expect("empty list is fine")
				.is_empty()
		);
	}

	#[test]
	fn a_bundle_yields_every_certificate() {
		let path = scratch("bundle.pem", &format!("{ROOT_PEM}{ROOT_PEM}"));
		let roots = trusted_roots(&[path.to_string_lossy().into_owned()]).expect("bundle parses");
		fs::remove_file(&path).ok();
		assert_eq!(roots.len(), 2);
	}

	#[test]
	fn a_missing_file_is_a_config_error_naming_the_path() {
		let path = std::env::temp_dir().join("tuwunel-trusted-roots-does-not-exist.pem");
		let err = trusted_roots(&[path.to_string_lossy().into_owned()])
			.expect_err("missing file fails");
		assert!(err.to_string().contains("does-not-exist"), "{err}");
	}

	#[test]
	fn a_file_without_certificates_is_rejected() {
		let path = scratch("empty.pem", "not a certificate\n");
		let result = trusted_roots(&[path.to_string_lossy().into_owned()]);
		fs::remove_file(&path).ok();
		result.expect_err("a file with no certificates fails");
	}
}
