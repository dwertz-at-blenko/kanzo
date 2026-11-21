//! Stamps the binary with the commit it was built from, so `kanzo --version` and
//! `/api/version` say exactly what is running; `deploy/deploy.sh` refuses a stamp that is
//! not the pushed commit. `-dirty` marks uncommitted changes to tracked files, and outside a
//! git checkout (a crates.io build) the commit is `unknown`.
//!
//! No `rerun-if` lines on purpose: Cargo then reruns this when any package file changes.
//! A new commit that changes no package file keeps the old stamp, which the deploy script
//! catches and fixes by touching this file.

use std::path::Path;
use std::process::Command;

fn git(args: &[&str]) -> Option<String> {
    let out = Command::new("git").args(args).output().ok().filter(|o| o.status.success())?;
    Some(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

fn main() {
    // Cargo runs this in the package folder; `.git` there (a folder, or a file in a linked
    // worktree) means this package is a checkout, not a copy inside some other repo.
    let sha = Path::new(".git").exists().then(|| git(&["rev-parse", "HEAD"])).flatten().filter(|s| s.len() >= 12);
    let (commit, short) = match sha {
        Some(sha) => {
            let clean = git(&["--no-optional-locks", "status", "--porcelain", "--untracked-files=no"])
                .is_some_and(|s| s.is_empty());
            let dirty = if clean { "" } else { "-dirty" };
            (format!("{sha}{dirty}"), format!("{}{dirty}", &sha[..12]))
        }
        None => ("unknown".to_string(), "unknown".to_string()),
    };
    println!("cargo:rustc-env=KANZO_COMMIT={commit}");
    println!("cargo:rustc-env=KANZO_LONG_VERSION={} ({short})", env!("CARGO_PKG_VERSION"));
}
