use std::{process::Command, str};

use proc_macro::TokenStream;
use quote::quote;

/// Only tags shaped like a version (`v1.2.3`) name a release. A downstream
/// tag with any other name would otherwise be picked up as the nearest tag,
/// and the `v` strip and trailing split below would turn it into nonsense.
const TAG_MATCH: &str = "--match=v[0-9]*";

pub(super) fn semantic(_args: TokenStream) -> TokenStream {
	static ARGS: &[&str] = &["describe", "--tags", "--abbrev=1", TAG_MATCH];

	let output = git(ARGS);
	let output = output
		.strip_prefix('v')
		.map(ToOwned::to_owned)
		.unwrap_or(output);

	let output = output
		.rsplit_once('-')
		.map(|(s, _)| s)
		.map(ToOwned::to_owned)
		.unwrap_or(output);

	let ret = quote! {
		static GIT_SEMANTIC: &'static str = #output;
	};

	ret.into()
}

pub(super) fn commit(_args: TokenStream) -> TokenStream {
	static ARGS: &[&str] = &["describe", "--always", "--dirty", "--abbrev=10", TAG_MATCH];

	let output = git(ARGS);
	let ret = quote! {
		static GIT_COMMIT: &'static str = #output;
	};

	ret.into()
}

fn git(args: &[&str]) -> String {
	Command::new("git")
		.args(args)
		.output()
		.map(|output| {
			str::from_utf8(&output.stdout)
				.map(str::trim)
				.map(String::from)
				.ok()
		})
		.ok()
		.flatten()
		.unwrap_or_default()
}
