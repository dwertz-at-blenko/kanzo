//! The database uses a rollback journal (`journal_mode = TRUNCATE`), not WAL. With a
//! connection per operation, WAL would set up and tear down its `-wal` and `-shm` files
//! on every request; the rollback journal makes a read touch no extra files at all and
//! measured about 3x faster per operation.

use std::collections::HashSet;
use std::fmt;
use std::path::Path;
use std::time::Duration;

use rusqlite::functions::FunctionFlags;
use rusqlite::types::Value;
use rusqlite::{Connection, OptionalExtension, Row, params};
use serde::Serialize;

/// The oldest schema version whose Kanzo can still use a database at the latest one.
/// Adding a table or a column with a default leaves older versions working, so a board
/// synced from a newer Kanzo still opens in an older one. Raise it to the new version
/// only for a migration that older code can't live with, like a renamed column.
const COMPATIBLE_FROM: usize = 4;

const MIGRATIONS: &[&str] = &[
    include_str!("schema.sql"),
    "ALTER TABLE projects ADD COLUMN color TEXT;",
    "ALTER TABLE tasks ADD COLUMN tags TEXT NOT NULL DEFAULT '';",
    // The old projects.repo column stays, empty: SQLite can only drop it by rebuilding the table.
    "CREATE TABLE project_repos (
         repo       TEXT PRIMARY KEY,
         project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE
     );
     CREATE INDEX project_repos_by_project ON project_repos(project_id);
     INSERT INTO project_repos (repo, project_id) SELECT repo, id FROM projects WHERE repo IS NOT NULL;
     UPDATE projects SET repo = NULL;
     CREATE TRIGGER project_repos_insert AFTER INSERT ON project_repos BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
     CREATE TRIGGER project_repos_delete AFTER DELETE ON project_repos BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;",
    "CREATE TABLE images (
         id         INTEGER PRIMARY KEY AUTOINCREMENT,
         task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
         comment_id INTEGER REFERENCES comments(id) ON DELETE CASCADE,
         file       TEXT NOT NULL,
         created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
     );
     CREATE INDEX images_by_task ON images(task_id);
     CREATE TRIGGER images_insert AFTER INSERT ON images BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
     CREATE TRIGGER images_delete AFTER DELETE ON images BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;",
    "ALTER TABLE tasks ADD COLUMN human INTEGER NOT NULL DEFAULT 0;",
    "ALTER TABLE tasks ADD COLUMN url TEXT NOT NULL DEFAULT '';",
];

const PROJECT_COLORS: &[&str] = &["gray", "red", "orange", "yellow", "green", "teal", "blue", "purple", "pink"];

const NOW: &str = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')";

#[derive(Debug)]
pub enum Error {
    NotFound(String),
    Invalid(String),
    Busy(String),
    Db(rusqlite::Error),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::NotFound(m) | Error::Invalid(m) | Error::Busy(m) => f.write_str(m),
            Error::Db(e) => write!(f, "database error: {e}"),
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Error::Db(e) => Some(e),
            _ => None,
        }
    }
}

impl From<rusqlite::Error> for Error {
    fn from(e: rusqlite::Error) -> Self {
        Error::Db(e)
    }
}

pub type Result<T> = std::result::Result<T, Error>;

#[derive(Debug, Clone, Serialize)]
pub struct Project {
    pub id: i64,
    pub slug: String,
    pub name: String,
    pub repos: Vec<String>,
    pub color: Option<String>,
    pub created_at: String,
    pub open: i64,
    pub total: i64,
    /// Open cards in review: the sidebar's "Needs you".
    pub review: i64,
    /// Open cards flagged for the human.
    pub waiting: i64,
    /// The newest task edit or comment in the project, "" when it has none.
    pub changed_at: String,
}

/// The latest comment on a card in the inbox, as one plain line.
#[derive(Debug, Clone, Serialize)]
pub struct Last {
    pub author: String,
    pub at: String,
    pub excerpt: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct InboxItem {
    #[serde(flatten)]
    pub task: Task,
    pub last: Option<Last>,
}

/// What waits on the human across every project: the board's Inbox view.
#[derive(Debug, Default, Serialize)]
pub struct Inbox {
    /// Open cards in review or flagged, where someone else wrote last.
    pub waiting: Vec<InboxItem>,
    /// The other cards in review.
    pub review: Vec<InboxItem>,
    /// The other open flagged cards; the page links to the filter instead of listing them.
    pub flagged_count: usize,
    /// Cards done in the last 3 days whose last comment is not the human's.
    pub finished: Vec<InboxItem>,
}

/// One browser's push subscription, as `PushSubscription.toJSON()` gives it, plus when it was
/// added and a label the page picks. Kept in `meta` under `push_subscriptions`.
#[derive(Debug, Clone, PartialEq, Serialize, serde::Deserialize)]
pub struct PushSubscription {
    pub endpoint: String,
    pub keys: PushKeys,
    #[serde(default)]
    pub added: String,
    #[serde(default)]
    pub label: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, serde::Deserialize)]
pub struct PushKeys {
    pub p256dh: String,
    pub auth: String,
}

/// Browsers kept; a new one pushes the oldest out.
pub const PUSH_KEEP: usize = 10;

/// Who the board signs comments as; anyone else is an agent or the dispatcher.
pub const ME: &str = "me";

#[derive(Debug, Clone, Serialize)]
pub struct Task {
    pub id: i64,
    pub project: String,
    pub title: String,
    pub description: String,
    pub tags: Vec<String>,
    /// A link to anything, like the task's pull request; empty when there is none.
    pub url: String,
    pub human: bool,
    pub status: String,
    pub position: f64,
    pub created_at: String,
    pub updated_at: String,
    pub comments: i64,
}

impl Task {
    pub fn key(&self) -> String {
        format!("{}#{}", self.project, self.id)
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct Comment {
    pub id: i64,
    pub task_id: i64,
    pub author: String,
    pub body: String,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct Image {
    pub id: i64,
    pub task_id: i64,
    pub comment_id: Option<i64>,
    pub file: String,
}

/// One row of the activity log: what happened to a task, a comment or a project, and when.
#[derive(Debug, Clone, Serialize)]
pub struct Activity {
    pub id: i64,
    pub at: String,
    /// Who did it, as far as Kanzo knows: "board", or an agent's client and folder.
    pub actor: Option<String>,
    pub action: String,
    pub task_id: Option<i64>,
    pub project: Option<String>,
    pub title: String,
    pub detail: String,
}

#[derive(Debug, Default)]
pub struct TaskPatch {
    pub title: Option<String>,
    pub description: Option<String>,
    pub status: Option<String>,
    pub project_id: Option<i64>,
    pub tags: Option<String>,
    pub url: Option<String>,
    pub human: Option<bool>,
}

const STATUSES: [&str; 4] = ["todo", "doing", "review", "done"];

pub const DONE: &str = STATUSES[3];

pub const BACKLOG: &str = "backlog";

/// The activity log's table and triggers. Not one of `MIGRATIONS`, so this fork and upstream
/// never number each other's steps; `Store::migrate` runs the file once per version of it.
const ACTIVITY_SQL: &str = include_str!("activity.sql");
const ACTIVITY_VERSION: &str = "1";
/// How many rows of the log one request returns.
pub const ACTIVITY_PAGE: usize = 100;

/// What a task shows besides its id, title and status; each can be turned off in Settings.
const PROPERTIES: [&str; 8] = ["description", "tags", "url", "human", "created", "updated", "comments", "images"];

pub struct Store {
    conn: Connection,
    statuses: Vec<String>,
    /// Who the activity log says made this store's writes.
    actor: Option<String>,
    /// Declared after `conn` so it is released after the connection closes.
    _lock: Option<std::fs::File>,
}

impl Store {
    pub fn open(path: &Path, lock: Option<std::fs::File>) -> Result<Self> {
        let conn = Connection::open(path)?;
        // The board and any number of agents share this file; wait instead of failing.
        conn.busy_timeout(Duration::from_secs(5))?;
        // Per connection, not stored in the file. Also turns a WAL database from an
        // older kanzo back into a rollback-journal one once nothing else has it open.
        // `synchronous` stays at SQLite's default FULL, which keeps the file intact
        // when a board loses power.
        conn.pragma_update_and_check(None, "journal_mode", "TRUNCATE", |_| Ok(()))?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        // Before the first migration there is no meta table, and so no backlog.
        let backlog = conn
            .query_row("SELECT value FROM meta WHERE key = 'backlog'", [], |r| r.get::<_, String>(0))
            .is_ok_and(|v| v == "1");
        let statuses = backlog.then_some(BACKLOG).into_iter().chain(STATUSES).map(String::from).collect();
        Ok(Self { conn, statuses, actor: None, _lock: lock })
    }

    /// Names the writer for the activity log.
    pub fn acting(mut self, actor: impl Into<String>) -> Self {
        self.actor = Some(actor.into());
        self
    }

    pub fn statuses(&self) -> &[String] {
        &self.statuses
    }

    pub fn set_backlog(&self, on: bool) -> Result<()> {
        self.write(|c| {
            let left: i64 = c.query_row("SELECT count(*) FROM tasks WHERE status = ?1", [BACKLOG], |r| r.get(0))?;
            if !on && left > 0 {
                return Err(Error::Invalid(format!("move the {left} {} out of backlog first", tasks(left))));
            }
            c.execute(
                "INSERT INTO meta (key, value) VALUES ('backlog', ?1) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [if on { "1" } else { "0" }],
            )?;
            // Other open boards redraw their columns.
            c.execute("UPDATE meta SET value = value + 1 WHERE key = 'changes'", [])?;
            Ok(())
        })
    }

    /// The look picked on the board, for every browser that opens it; `None` until one is picked.
    pub fn theme(&self) -> Result<Option<String>> {
        Ok(self.conn.query_row("SELECT value FROM meta WHERE key = 'theme'", [], |r| r.get(0)).optional()?)
    }

    /// Keeps the look picked on the board. The caller checks the name is a theme.
    pub fn set_theme(&self, theme: &str) -> Result<()> {
        self.write(|c| {
            c.execute(
                "INSERT INTO meta (key, value) VALUES ('theme', ?1) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [theme],
            )?;
            // Other open boards switch too.
            c.execute("UPDATE meta SET value = value + 1 WHERE key = 'changes'", [])?;
            Ok(())
        })
    }

    /// The properties turned off in Settings: off the board, and out of what agents get.
    pub fn hidden_properties(&self) -> Result<Vec<String>> {
        let hidden: Option<String> = self
            .conn
            .query_row("SELECT value FROM meta WHERE key = 'hidden_properties'", [], |r| r.get(0))
            .optional()?;
        Ok(hidden.unwrap_or_default().split(',').filter(|p| !p.is_empty()).map(String::from).collect())
    }

    pub fn set_property(&self, property: &str, visible: bool) -> Result<()> {
        if !PROPERTIES.contains(&property) {
            return Err(Error::Invalid(format!("unknown property {property:?}")));
        }
        self.write(|c| {
            let mut hidden = self.hidden_properties()?;
            hidden.retain(|p| p != property);
            if !visible {
                hidden.push(property.to_string());
            }
            c.execute(
                "INSERT INTO meta (key, value) VALUES ('hidden_properties', ?1) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [hidden.join(",")],
            )?;
            // Other open boards redraw their tasks.
            c.execute("UPDATE meta SET value = value + 1 WHERE key = 'changes'", [])?;
            Ok(())
        })
    }

    /// The projects starred in the sidebar, for every browser. A slug whose project is gone
    /// drops out here, so deleting or renaming a project needs no cleanup.
    pub fn starred(&self) -> Result<Vec<String>> {
        let saved: Option<String> = self
            .conn
            .query_row("SELECT value FROM meta WHERE key = 'starred_projects'", [], |r| r.get(0))
            .optional()?;
        let mut stmt = self.conn.prepare("SELECT slug FROM projects")?;
        let known: HashSet<String> = stmt.query_map([], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
        Ok(saved.unwrap_or_default().split(',').filter(|s| known.contains(*s)).map(String::from).collect())
    }

    pub fn set_starred(&self, slug: &str, starred: bool) -> Result<()> {
        let slug = self.find_project(slug)?.slug;
        self.write(|c| {
            let mut stars = self.starred()?;
            stars.retain(|s| *s != slug);
            if starred {
                stars.push(slug);
            }
            c.execute(
                "INSERT INTO meta (key, value) VALUES ('starred_projects', ?1) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [stars.join(",")],
            )?;
            // Other open boards move the project too.
            c.execute("UPDATE meta SET value = value + 1 WHERE key = 'changes'", [])?;
            Ok(())
        })
    }

    /// The browsers that asked for a push when an agent answers. Their endpoints are capability
    /// URLs: kanzo-dispatch reads this row read-only from the file, and no route returns it.
    pub fn push_subscriptions(&self) -> Result<Vec<PushSubscription>> {
        let saved: Option<String> = self
            .conn
            .query_row("SELECT value FROM meta WHERE key = 'push_subscriptions'", [], |r| r.get(0))
            .optional()?;
        Ok(saved.and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default())
    }

    /// Adds one browser, newest first; the same endpoint again replaces the old entry, and only
    /// the newest `PUSH_KEEP` stay. The caller checks the endpoint (`web::push_endpoint_ok`).
    pub fn add_push_subscription(&self, endpoint: &str, p256dh: &str, auth: &str, label: &str) -> Result<usize> {
        self.write(|c| {
            let added: String = c.query_row("SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now')", [], |r| r.get(0))?;
            let mut subs = self.push_subscriptions()?;
            subs.retain(|s| s.endpoint != endpoint);
            subs.insert(
                0,
                PushSubscription {
                    endpoint: endpoint.into(),
                    keys: PushKeys { p256dh: p256dh.into(), auth: auth.into() },
                    added,
                    label: label.into(),
                },
            );
            subs.truncate(PUSH_KEEP);
            Self::save_push(c, &subs)?;
            Ok(subs.len())
        })
    }

    pub fn remove_push_subscription(&self, endpoint: &str) -> Result<usize> {
        self.write(|c| {
            let mut subs = self.push_subscriptions()?;
            subs.retain(|s| s.endpoint != endpoint);
            Self::save_push(c, &subs)?;
            Ok(subs.len())
        })
    }

    // No change token bump: no board redraws for this.
    fn save_push(c: &Connection, subs: &[PushSubscription]) -> Result<()> {
        let json = serde_json::to_string(subs).map_err(|e| Error::Invalid(e.to_string()))?;
        c.execute(
            "INSERT INTO meta (key, value) VALUES ('push_subscriptions', ?1) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [json],
        )?;
        Ok(())
    }

    /// Seconds from `at` (an ISO time) to now; `None` when SQLite cannot read `at` as a time.
    pub fn seconds_since(&self, at: &str) -> Result<Option<i64>> {
        Ok(self.conn.query_row(
            "SELECT CAST(strftime('%s', 'now') AS INTEGER) - CAST(strftime('%s', ?1) AS INTEGER)",
            [at],
            |r| r.get(0),
        )?)
    }

    /// Immediate, so concurrent writers queue up on the busy timeout instead of failing halfway.
    fn write<T>(&self, f: impl FnOnce(&Connection) -> Result<T>) -> Result<T> {
        self.conn.execute_batch("BEGIN IMMEDIATE")?;
        // Triggers write the activity log and cannot know who is writing, so note where the
        // log ends and sign what this write adds to it. No log yet: nothing to sign.
        let mark = self.actor.as_ref().and_then(|_| {
            self.conn
                .prepare_cached("SELECT coalesce(max(id), 0) FROM activity")
                .and_then(|mut q| q.query_row([], |r| r.get::<_, i64>(0)))
                .ok()
        });
        let signed = f(&self.conn).and_then(|v| {
            if let (Some(actor), Some(mark)) = (&self.actor, mark) {
                self.conn
                    .prepare_cached("UPDATE activity SET actor = ?1 WHERE id > ?2 AND actor IS NULL")?
                    .execute(params![actor, mark])?;
            }
            Ok(v)
        });
        match signed {
            Ok(v) => {
                self.conn.execute_batch("COMMIT")?;
                Ok(v)
            }
            Err(e) => {
                let _ = self.conn.execute_batch("ROLLBACK");
                Err(e)
            }
        }
    }

    /// Writes nothing when up to date, so starting `kanzo mcp` for every agent session
    /// costs no disk write.
    pub fn migrate(&self) -> Result<()> {
        let meta = |c: &Connection, key: &str| -> rusqlite::Result<Option<usize>> {
            Ok(c.query_row("SELECT value FROM meta WHERE key = ?1", [key], |r| r.get::<_, String>(0))
                .optional()?
                .and_then(|v| v.parse().ok()))
        };
        let latest = MIGRATIONS.len();
        let current =
            (meta(&self.conn, "schema_version").unwrap_or(None), meta(&self.conn, "compatible").unwrap_or(None));
        if current == (Some(latest), Some(COMPATIBLE_FROM)) {
            return self.activity_log();
        }
        self.write(|c| {
            c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)", [])?;
            let version = meta(c, "schema_version")?.unwrap_or(0);
            if version > latest {
                // From a newer Kanzo, maybe synced from another machine: use it as it is
                // when this version can, without touching its schema.
                if meta(c, "compatible")?.is_some_and(|oldest| oldest <= latest) {
                    return Ok(());
                }
                return Err(Error::Invalid(
                    "this board was saved by a newer version of Kanzo; update this one with `kanzo update`".into(),
                ));
            }
            for sql in &MIGRATIONS[version..] {
                c.execute_batch(sql)?;
            }
            c.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?1), ('compatible', ?2)
                 ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [latest.to_string(), COMPATIBLE_FROM.to_string()],
            )?;
            Ok(())
        })?;
        self.activity_log()
    }

    /// Sets up the activity log, or brings it to this version. One read when it is current.
    fn activity_log(&self) -> Result<()> {
        let version = |c: &Connection| -> rusqlite::Result<Option<String>> {
            c.query_row("SELECT value FROM meta WHERE key = 'activity'", [], |r| r.get(0)).optional()
        };
        if version(&self.conn)?.as_deref() == Some(ACTIVITY_VERSION) {
            return Ok(());
        }
        self.write(|c| {
            if version(c)?.as_deref() != Some(ACTIVITY_VERSION) {
                c.execute_batch(ACTIVITY_SQL)?;
                c.execute(
                    "INSERT INTO meta (key, value) VALUES ('activity', ?1)
                     ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    [ACTIVITY_VERSION],
                )?;
            }
            Ok(())
        })
    }

    /// The activity log, newest first: one page ending before row `before`, for one project
    /// or for all of them.
    pub fn list_activity(&self, before: Option<i64>, project: Option<&str>) -> Result<Vec<Activity>> {
        let mut q = self.conn.prepare_cached(
            "SELECT id, at, actor, action, task_id, project, title, detail FROM activity
             WHERE (?1 IS NULL OR id < ?1) AND (?2 IS NULL OR project = ?2)
             ORDER BY id DESC LIMIT ?3",
        )?;
        let rows = q.query_map(params![before, project, ACTIVITY_PAGE as i64], |r| {
            Ok(Activity {
                id: r.get(0)?,
                at: r.get(1)?,
                actor: r.get(2)?,
                action: r.get(3)?,
                task_id: r.get(4)?,
                project: r.get(5)?,
                title: r.get(6)?,
                detail: r.get(7)?,
            })
        })?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    pub fn changes(&self) -> Result<i64> {
        Ok(self
            .conn
            .prepare_cached("SELECT CAST(value AS INTEGER) FROM meta WHERE key = 'changes'")?
            .query_row([], |r| r.get(0))?)
    }

    pub fn now(&self) -> Result<String> {
        Ok(self.conn.query_row(&format!("SELECT {NOW}"), [], |r| r.get(0))?)
    }

    /// Makes `kanzo.db` complete on its own: rolls back a write that a crashed process
    /// left half done, and folds in a WAL left by an older kanzo.
    pub fn settle(&self) -> Result<()> {
        self.conn.query_row("SELECT count(*) FROM meta", [], |r| r.get::<_, i64>(0))?;
        Ok(())
    }

    const PROJECT_SELECT: &str = "SELECT p.id, p.slug, p.name,
            (SELECT group_concat(repo, ',' ORDER BY r.rowid) FROM project_repos r WHERE r.project_id = p.id),
            p.color, p.created_at,
            (SELECT count(*) FROM tasks t WHERE t.project_id = p.id AND t.status <> ?1),
            (SELECT count(*) FROM tasks t WHERE t.project_id = p.id),
            (SELECT count(*) FROM tasks t WHERE t.project_id = p.id AND t.status = 'review'),
            (SELECT count(*) FROM tasks t WHERE t.project_id = p.id AND t.human AND t.status <> ?1),
            max(coalesce((SELECT max(t.updated_at) FROM tasks t WHERE t.project_id = p.id), ''),
                coalesce((SELECT max(c.created_at) FROM comments c JOIN tasks t ON t.id = c.task_id
                          WHERE t.project_id = p.id), ''))
        FROM projects p";

    fn project_row(r: &Row) -> rusqlite::Result<Project> {
        Ok(Project {
            id: r.get(0)?,
            slug: r.get(1)?,
            name: r.get(2)?,
            repos: r.get::<_, Option<String>>(3)?.map(|s| s.split(',').map(String::from).collect()).unwrap_or_default(),
            color: r.get(4)?,
            created_at: r.get(5)?,
            open: r.get(6)?,
            total: r.get(7)?,
            review: r.get(8)?,
            waiting: r.get(9)?,
            changed_at: r.get(10)?,
        })
    }

    pub fn list_projects(&self) -> Result<Vec<Project>> {
        let sql = format!("{} ORDER BY p.slug", Self::PROJECT_SELECT);
        let mut stmt = self.conn.prepare(&sql)?;
        let rows = stmt.query_map([DONE], Self::project_row)?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    fn project_where(&self, clause: &str, value: &str) -> Result<Option<Project>> {
        let sql = format!("{} WHERE {clause}", Self::PROJECT_SELECT);
        Ok(self.conn.query_row(&sql, params![DONE, value], Self::project_row).optional()?)
    }

    pub fn project_by_slug(&self, slug: &str) -> Result<Option<Project>> {
        self.project_where("p.slug = ?2", &slug.trim().to_ascii_lowercase())
    }

    pub fn project_by_repo(&self, repo: &str) -> Result<Option<Project>> {
        match repo_from_url(repo) {
            Some(repo) => self.project_where("p.id = (SELECT project_id FROM project_repos WHERE repo = ?2)", &repo),
            None => Ok(None),
        }
    }

    pub fn find_project(&self, name: &str) -> Result<Project> {
        let found = match self.project_by_slug(name)? {
            Some(p) => Some(p),
            None if name.contains('/') => self.project_by_repo(name)?,
            None => None,
        };
        found.ok_or_else(|| {
            let known = self.list_projects().unwrap_or_default();
            let names: Vec<_> = known.iter().map(|p| p.slug.as_str()).collect();
            let hint = if names.is_empty() {
                "there are no projects yet".to_string()
            } else {
                format!("projects: {}", names.join(", "))
            };
            Error::NotFound(format!("no project {name:?} ({hint})"))
        })
    }

    pub fn create_project(&self, slug: &str, name: &str, repo: Option<&str>) -> Result<Project> {
        let slug = slug.trim().to_ascii_lowercase();
        if !valid_slug(&slug) {
            return Err(Error::Invalid(format!(
                "project slug {slug:?} must be 1-32 lowercase letters, digits or -, starting with a letter or digit"
            )));
        }
        let name = match name.trim() {
            "" => slug.clone(),
            n => n.to_string(),
        };
        let repo = repo.map(str::trim).filter(|r| !r.is_empty()).map(clean_repo).transpose()?;
        // Checked in the write's transaction, so nobody can take the slug or the repo in between.
        self.write(|c| {
            if self.project_by_slug(&slug)?.is_some() {
                return Err(Error::Invalid(format!("project {slug} already exists")));
            }
            if let Some(r) = &repo {
                self.check_repo_free(r)?;
            }
            c.execute("INSERT INTO projects (slug, name) VALUES (?1, ?2)", params![slug, name])?;
            if let Some(r) = &repo {
                c.execute(
                    "INSERT INTO project_repos (repo, project_id) VALUES (?1, ?2)",
                    params![r, c.last_insert_rowid()],
                )?;
            }
            Ok(())
        })?;
        self.find_project(&slug)
    }

    pub fn link_repo(&self, slug: &str, repo: &str) -> Result<Project> {
        let project = self.find_project(slug)?;
        let repo = clean_repo(repo)?;
        if project.repos.contains(&repo) {
            return Ok(project);
        }
        self.write(|c| {
            self.check_repo_free(&repo)?;
            c.execute("INSERT INTO project_repos (repo, project_id) VALUES (?1, ?2)", params![repo, project.id])?;
            Ok(())
        })?;
        self.find_project(&project.slug)
    }

    pub fn unlink_repo(&self, slug: &str, repo: &str) -> Result<Project> {
        let project = self.find_project(slug)?;
        let repo = repo_from_url(repo).unwrap_or_else(|| repo.trim().to_ascii_lowercase());
        let removed = self
            .conn
            .execute("DELETE FROM project_repos WHERE repo = ?1 AND project_id = ?2", params![repo, project.id])?;
        if removed == 0 {
            return Err(Error::NotFound(format!("repo {repo} is not linked to project {}", project.slug)));
        }
        self.find_project(&project.slug)
    }

    fn check_repo_free(&self, repo: &str) -> Result<()> {
        match self.project_by_repo(repo)? {
            Some(other) => Err(Error::Invalid(format!("repo {repo} is already linked to project {}", other.slug))),
            None => Ok(()),
        }
    }

    pub fn rename_project(&self, slug: &str, name: &str) -> Result<Project> {
        let name = squash(name);
        if name.is_empty() {
            return Err(Error::Invalid("project name is empty".into()));
        }
        let project = self.find_project(slug)?;
        self.conn.execute("UPDATE projects SET name = ?1 WHERE id = ?2", params![name, project.id])?;
        self.find_project(&project.slug)
    }

    pub fn set_project_color(&self, slug: &str, color: &str) -> Result<Project> {
        if !PROJECT_COLORS.contains(&color) {
            return Err(Error::Invalid(format!("unknown color {color:?}; use one of: {}", PROJECT_COLORS.join(", "))));
        }
        let project = self.find_project(slug)?;
        self.conn.execute("UPDATE projects SET color = ?1 WHERE id = ?2", params![color, project.id])?;
        self.find_project(&project.slug)
    }

    pub fn delete_project(&self, slug: &str) -> Result<Project> {
        let project = self.find_project(slug)?;
        // Comments go with the tasks through ON DELETE CASCADE. The tasks go first, by hand,
        // so the activity log can still name their project.
        self.write(|c| {
            c.execute("DELETE FROM tasks WHERE project_id = ?1", [project.id])?;
            c.execute("DELETE FROM projects WHERE id = ?1", [project.id])?;
            Ok(())
        })?;
        Ok(project)
    }

    const TASK_SELECT: &str = "SELECT t.id, p.slug, t.title, t.description, t.status, t.position,
            t.created_at, t.updated_at, (SELECT count(*) FROM comments c WHERE c.task_id = t.id), t.tags, t.human,
            t.url
        FROM tasks t JOIN projects p ON p.id = t.project_id";

    fn task_row(r: &Row) -> rusqlite::Result<Task> {
        Ok(Task {
            id: r.get(0)?,
            project: r.get(1)?,
            title: r.get(2)?,
            description: r.get(3)?,
            tags: r.get::<_, String>(9)?.split(',').filter(|t| !t.is_empty()).map(String::from).collect(),
            url: r.get(11)?,
            human: r.get(10)?,
            status: r.get(4)?,
            position: r.get(5)?,
            created_at: r.get(6)?,
            updated_at: r.get(7)?,
            comments: r.get(8)?,
        })
    }

    pub fn list_tasks(&self, project_id: Option<i64>, status: Option<&str>) -> Result<Vec<Task>> {
        Ok(self.list_tasks_keeping_done(project_id, status, None)?.0)
    }

    /// The done column only grows, so only its newest `keep_done` tasks are read.
    pub fn list_tasks_keeping_done(
        &self,
        project_id: Option<i64>,
        status: Option<&str>,
        keep_done: Option<usize>,
    ) -> Result<(Vec<Task>, usize)> {
        let done = DONE;
        // SQLite reads a negative LIMIT as no limit.
        let limit = keep_done.map_or(-1, |n| n as i64);
        let sql = format!(
            "{} WHERE (?1 IS NULL OR t.project_id = ?1) AND (?2 IS NULL OR t.status = ?2)
               AND (t.status <> ?3 OR t.id IN (SELECT id FROM tasks WHERE status = ?3
                    AND (?1 IS NULL OR project_id = ?1) ORDER BY position DESC LIMIT ?4))
             ORDER BY t.position",
            Self::TASK_SELECT
        );
        let mut stmt = self.conn.prepare(&sql)?;
        let mut tasks: Vec<Task> = stmt
            .query_map(params![project_id, status, done, limit], Self::task_row)?
            .collect::<rusqlite::Result<_>>()?;
        let column = |t: &Task| self.statuses().iter().position(|s| *s == t.status).unwrap_or(usize::MAX);
        tasks.sort_by_key(column);
        let hidden = match keep_done {
            Some(keep) if status.is_none_or(|s| s == done) => {
                let all: i64 = self.conn.query_row(
                    "SELECT count(*) FROM tasks WHERE status = ?1 AND (?2 IS NULL OR project_id = ?2)",
                    params![done, project_id],
                    |r| r.get(0),
                )?;
                usize::try_from(all).unwrap_or(0).saturating_sub(keep)
            }
            _ => 0,
        };
        Ok((tasks, hidden))
    }

    // SQLite's LIKE ignores case only for ASCII. Registered per query, not in open, because
    // every operation opens a connection and only filters need this.
    fn add_has_word(&self) -> Result<()> {
        let flags = FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC;
        self.conn.create_scalar_function("has_word", 2, flags, |c| {
            Ok(c.get_raw(0).as_str()?.to_lowercase().contains(c.get_raw(1).as_str()?))
        })?;
        Ok(())
    }

    pub fn search(&self, query: &str, limit: Option<usize>) -> Result<Vec<Task>> {
        self.search_seeing(query, limit, &[])
    }

    /// Finds nothing through a property Settings hides from agents.
    pub fn search_for_agent(&self, query: &str) -> Result<Vec<Task>> {
        self.search_seeing(query, None, &self.hidden_properties()?)
    }

    fn search_seeing(&self, query: &str, limit: Option<usize>, hidden: &[String]) -> Result<Vec<Task>> {
        self.add_has_word()?;
        let filter = Filter::new(query, 2, hidden);
        // SQLite reads a negative LIMIT as no limit.
        let limit = limit.map_or(-1, |n| n as i64);
        let sql = format!(
            "{} WHERE 1{} ORDER BY t.status = ?1, t.updated_at DESC, t.id DESC LIMIT {limit}",
            Self::TASK_SELECT,
            filter.sql
        );
        let mut values: Vec<String> = vec![DONE.to_string()];
        values.extend(filter.values);
        let mut stmt = self.conn.prepare(&sql)?;
        let rows = stmt.query_map(rusqlite::params_from_iter(values.iter()), Self::task_row)?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    pub fn get_task(&self, id: i64) -> Result<Task> {
        let sql = format!("{} WHERE t.id = ?1", Self::TASK_SELECT);
        self.conn
            .prepare_cached(&sql)?
            .query_row([id], Self::task_row)
            .optional()?
            .ok_or_else(|| Error::NotFound(format!("no task #{id}")))
    }

    /// The cards with these ids that still exist, in one read.
    pub fn tasks_by_ids(&self, ids: &[i64]) -> Result<Vec<Task>> {
        let sql = format!("{} WHERE t.id IN (SELECT value FROM json_each(?1))", Self::TASK_SELECT);
        let ids = format!("[{}]", ids.iter().map(i64::to_string).collect::<Vec<_>>().join(","));
        let mut stmt = self.conn.prepare(&sql)?;
        let rows = stmt.query_map([ids], Self::task_row)?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    /// The Inbox view in one read: each card with its latest comment, sorted into groups by
    /// SQL. Done cards older than 3 days never leave the database.
    pub fn inbox(&self) -> Result<Inbox> {
        let sql = format!(
            "SELECT * FROM (
                SELECT x.*, l.author AS last_author, l.created_at AS last_at, l.body,
                    CASE WHEN x.status <> ?1 AND (x.human OR x.status = 'review') AND l.author <> ?2 THEN 'waiting'
                         WHEN x.status = 'review' THEN 'review'
                         WHEN x.status <> ?1 AND x.human THEN 'flagged'
                         WHEN x.status = ?1 AND l.author <> ?2 THEN 'finished' END AS kind
                FROM ({} WHERE t.status <> ?1 OR t.updated_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-3 days')) x
                LEFT JOIN comments l ON l.id = (SELECT max(c.id) FROM comments c WHERE c.task_id = x.id))
             WHERE kind IS NOT NULL
             ORDER BY CASE kind WHEN 'finished' THEN updated_at ELSE coalesce(last_at, updated_at) END DESC, id DESC",
            Self::TASK_SELECT
        );
        let mut inbox = Inbox::default();
        let mut stmt = self.conn.prepare(&sql)?;
        let mut rows = stmt.query(params![DONE, ME])?;
        while let Some(r) = rows.next()? {
            let last = match r.get::<_, Option<String>>(12)? {
                Some(author) => Some(Last { author, at: r.get(13)?, excerpt: excerpt(&r.get::<_, String>(14)?) }),
                None => None,
            };
            let item = InboxItem { task: Self::task_row(r)?, last };
            match r.get::<_, String>(15)?.as_str() {
                "waiting" => inbox.waiting.push(item),
                "review" => inbox.review.push(item),
                "flagged" => inbox.flagged_count += 1,
                _ if inbox.finished.len() < 20 => inbox.finished.push(item),
                _ => {}
            }
        }
        Ok(inbox)
    }

    pub fn find_task(&self, key: &str) -> Result<Task> {
        let (slug, id) = parse_task_key(key)?;
        let task = self.get_task(id)?;
        if let Some(slug) = slug
            && slug != task.project
        {
            return Err(Error::NotFound(format!("no task {key} (#{id} is {})", task.key())));
        }
        Ok(task)
    }

    pub fn check_status(&self, status: &str) -> Result<()> {
        if self.statuses().iter().any(|s| s == status) {
            Ok(())
        } else {
            Err(Error::Invalid(format!("unknown status {status:?}; use one of: {}", self.statuses().join(", "))))
        }
    }

    fn end_of_column(c: &Connection, status: &str, except: i64) -> Result<f64> {
        let max: Option<f64> = c.query_row(
            "SELECT max(position) FROM tasks WHERE status = ?1 AND id <> ?2",
            params![status, except],
            |r| r.get(0),
        )?;
        Ok(max.map_or(1.0, |m| m.floor() + 1.0))
    }

    #[cfg(test)]
    pub fn create_task(&self, project: i64, title: &str, description: &str) -> Result<Task> {
        self.create_task_in(project, title, TaskPatch { description: Some(description.into()), ..Default::default() })
    }

    // One write, or an agent could take a task before it's human. Uses description, status, tags, human.
    pub fn create_task_in(&self, project_id: i64, title: &str, patch: TaskPatch) -> Result<Task> {
        let title = clean_title(title)?;
        let status = patch.status.as_deref().unwrap_or(STATUSES[0]);
        self.check_status(status)?;
        let description = patch.description.as_deref().unwrap_or("").trim();
        let tags = clean_tags(patch.tags.as_deref().unwrap_or("")).join(",");
        let id = self.write(|c| {
            let position = Self::end_of_column(c, status, 0)?;
            c.execute(
                "INSERT INTO tasks (project_id, title, description, status, position, tags, human) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                params![project_id, title, description, status, position, tags, patch.human.unwrap_or(false)],
            )?;
            Ok(c.last_insert_rowid())
        })?;
        self.get_task(id)
    }

    pub fn update_task(&self, id: i64, patch: TaskPatch) -> Result<Task> {
        let title = patch.title.as_deref().map(clean_title).transpose()?;
        let url = patch.url.as_deref().map(clean_url).transpose()?;
        if let Some(s) = &patch.status {
            self.check_status(s)?;
        }
        self.write(|c| {
            let current = self.get_task(id)?;
            // Only what the patch changes is written, and only then is the task newer.
            let tags = patch.tags.as_deref().map(|t| clean_tags(t).join(","));
            c.execute(
                &format!(
                    "UPDATE tasks SET title = coalesce(?1, title), project_id = coalesce(?2, project_id),
                       human = coalesce(?3, human), tags = coalesce(?4, tags),
                       description = coalesce(?5, description), url = coalesce(?7, url), updated_at = {NOW}
                     WHERE id = ?6 AND (coalesce(?1, title), coalesce(?2, project_id), coalesce(?3, human),
                       coalesce(?4, tags), coalesce(?5, description), coalesce(?7, url))
                       <> (title, project_id, human, tags, description, url)"
                ),
                params![
                    title,
                    patch.project_id,
                    patch.human,
                    tags,
                    patch.description.as_deref().map(str::trim),
                    id,
                    url
                ],
            )?;
            if let Some(s) = &patch.status
                && *s != current.status
            {
                let position = Self::end_of_column(c, s, id)?;
                c.execute(
                    &format!("UPDATE tasks SET status = ?1, position = ?2, updated_at = {NOW} WHERE id = ?3"),
                    params![s, position, id],
                )?;
            }
            Ok(())
        })?;
        self.get_task(id)
    }

    pub fn move_task(&self, id: i64, status: &str, before: Option<i64>) -> Result<Task> {
        self.check_status(status)?;
        self.write(|c| {
            let current = self.get_task(id)?;
            let before = before.filter(|b| *b != id);
            let anchor: Option<(i64, f64)> = match before {
                Some(b) => c
                    .query_row("SELECT position FROM tasks WHERE id = ?1 AND status = ?2", params![b, status], |r| {
                        r.get(0)
                    })
                    .optional()?
                    .map(|p| (b, p)),
                None => None,
            };
            let position = match anchor {
                None => Self::end_of_column(c, status, id)?,
                Some((before, mut next)) => {
                    let prev_of = |next: f64| -> Result<Option<f64>> {
                        Ok(c.query_row(
                            "SELECT max(position) FROM tasks WHERE status = ?1 AND position < ?2 AND id <> ?3",
                            params![status, next, id],
                            |r| r.get(0),
                        )?)
                    };
                    let mut prev = prev_of(next)?;
                    if prev.is_some_and(|p| next - p < 1e-9) {
                        // Out of float room between two neighbours.
                        Self::renumber(c, status)?;
                        next = c.query_row("SELECT position FROM tasks WHERE id = ?1", [before], |r| r.get(0))?;
                        prev = prev_of(next)?;
                    }
                    match prev {
                        Some(p) => (p + next) / 2.0,
                        None => next - 1.0,
                    }
                }
            };
            let touched = if current.status != status { format!(", updated_at = {NOW}") } else { String::new() };
            c.execute(
                &format!("UPDATE tasks SET status = ?1, position = ?2{touched} WHERE id = ?3"),
                params![status, position, id],
            )?;
            Ok(())
        })?;
        self.get_task(id)
    }

    fn renumber(c: &Connection, status: &str) -> Result<()> {
        let ids: Vec<i64> = c
            .prepare("SELECT id FROM tasks WHERE status = ?1 ORDER BY position")?
            .query_map([status], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        let mut update = c.prepare("UPDATE tasks SET position = ?1 WHERE id = ?2")?;
        for (i, id) in ids.iter().enumerate() {
            update.execute(params![(i + 1) as f64, id])?;
        }
        Ok(())
    }

    #[cfg(test)]
    pub fn next_task(&self, project_id: Option<i64>) -> Result<Option<Task>> {
        self.next_task_matching(project_id, "")
    }

    pub fn next_task_matching(&self, project_id: Option<i64>, query: &str) -> Result<Option<Task>> {
        let filter = if query.trim().is_empty() {
            Filter::default()
        } else {
            self.add_has_word()?;
            Filter::new(query, 3, &self.hidden_properties()?)
        };
        let [from, to, ..] = STATUSES;
        let id = self.write(|c| {
            let sql = format!(
                "SELECT t.id FROM tasks t JOIN projects p ON p.id = t.project_id
                 WHERE t.status = ?1 AND t.human = 0 AND (?2 IS NULL OR t.project_id = ?2){}
                 ORDER BY t.position LIMIT 1",
                filter.sql
            );
            let mut values: Vec<Value> = vec![from.to_string().into(), project_id.into()];
            values.extend(filter.values.iter().cloned().map(Value::from));
            let id: Option<i64> = c.query_row(&sql, rusqlite::params_from_iter(values), |r| r.get(0)).optional()?;
            if let Some(id) = id {
                let position = Self::end_of_column(c, to, id)?;
                c.execute(
                    &format!("UPDATE tasks SET status = ?1, position = ?2, updated_at = {NOW} WHERE id = ?3"),
                    params![to, position, id],
                )?;
            }
            Ok(id)
        })?;
        id.map(|id| self.get_task(id)).transpose()
    }

    pub fn delete_task(&self, id: i64) -> Result<Task> {
        let task = self.get_task(id)?;
        self.write(|c| Ok(c.execute("DELETE FROM tasks WHERE id = ?1", [id])?))?;
        Ok(task)
    }

    pub fn list_comments(&self, task_id: i64) -> Result<Vec<Comment>> {
        let mut stmt = self.conn.prepare_cached(&format!("{} WHERE task_id = ?1 ORDER BY id", Self::COMMENT_SELECT))?;
        let rows = stmt.query_map([task_id], Self::comment_row)?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    const COMMENT_SELECT: &str = "SELECT id, task_id, author, body, created_at FROM comments";

    fn comment_row(r: &Row) -> rusqlite::Result<Comment> {
        Ok(Comment { id: r.get(0)?, task_id: r.get(1)?, author: r.get(2)?, body: r.get(3)?, created_at: r.get(4)? })
    }

    pub fn add_comment(&self, task_id: i64, author: &str, body: &str, images: &[String]) -> Result<Comment> {
        let body = body.trim();
        if body.is_empty() && images.is_empty() {
            return Err(Error::Invalid("comment body is empty".into()));
        }
        images.iter().try_for_each(|f| check_image(f))?;
        let author = match author.trim() {
            "" => "me",
            a => a,
        };
        self.get_task(task_id)?;
        let id = self.write(|c| {
            c.execute(
                "INSERT INTO comments (task_id, author, body) VALUES (?1, ?2, ?3)",
                params![task_id, author, body],
            )?;
            let id = c.last_insert_rowid();
            let mut insert = c.prepare("INSERT INTO images (task_id, comment_id, file) VALUES (?1, ?2, ?3)")?;
            for file in images {
                insert.execute(params![task_id, id, file])?;
            }
            Ok(id)
        })?;
        self.get_comment(id)
    }

    fn get_comment(&self, id: i64) -> Result<Comment> {
        self.conn
            .query_row(&format!("{} WHERE id = ?1", Self::COMMENT_SELECT), [id], Self::comment_row)
            .optional()?
            .ok_or_else(|| Error::NotFound(format!("no comment {id}")))
    }

    pub fn delete_comment(&self, id: i64) -> Result<Comment> {
        let comment = self.get_comment(id)?;
        self.write(|c| Ok(c.execute("DELETE FROM comments WHERE id = ?1", [id])?))?;
        Ok(comment)
    }

    pub fn add_image(&self, task_id: i64, file: &str) -> Result<Image> {
        check_image(file)?;
        self.get_task(task_id)?;
        self.conn.execute("INSERT INTO images (task_id, file) VALUES (?1, ?2)", params![task_id, file])?;
        Ok(Image { id: self.conn.last_insert_rowid(), task_id, comment_id: None, file: file.into() })
    }

    pub fn list_images(&self, task_id: i64) -> Result<Vec<Image>> {
        let mut stmt = self.conn.prepare_cached(&format!("{} WHERE task_id = ?1 ORDER BY id", Self::IMAGE_SELECT))?;
        let rows = stmt.query_map([task_id], Self::image_row)?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    const IMAGE_SELECT: &str = "SELECT id, task_id, comment_id, file FROM images";

    fn image_row(r: &Row) -> rusqlite::Result<Image> {
        Ok(Image { id: r.get(0)?, task_id: r.get(1)?, comment_id: r.get(2)?, file: r.get(3)? })
    }

    pub fn delete_image(&self, id: i64) -> Result<Image> {
        let image = self
            .conn
            .query_row(&format!("{} WHERE id = ?1", Self::IMAGE_SELECT), [id], Self::image_row)
            .optional()?
            .ok_or_else(|| Error::NotFound(format!("no image {id}")))?;
        self.conn.execute("DELETE FROM images WHERE id = ?1", [id])?;
        Ok(image)
    }

    pub fn image_files(&self, of: ImagesOf) -> Result<Vec<String>> {
        let (sql, id) = match of {
            ImagesOf::Image(id) => ("SELECT file FROM images WHERE id = ?1", id),
            ImagesOf::Comment(id) => ("SELECT DISTINCT file FROM images WHERE comment_id = ?1", id),
            ImagesOf::Task(id) => ("SELECT DISTINCT file FROM images WHERE task_id = ?1", id),
            ImagesOf::Project(id) => {
                ("SELECT DISTINCT i.file FROM images i JOIN tasks t ON t.id = i.task_id WHERE t.project_id = ?1", id)
            }
        };
        let mut stmt = self.conn.prepare_cached(sql)?;
        let rows = stmt.query_map([id], |r| r.get(0))?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    pub fn unused_images(&self, files: Vec<String>) -> Result<Vec<String>> {
        let mut used = self.conn.prepare_cached("SELECT EXISTS (SELECT 1 FROM images WHERE file = ?1)")?;
        let mut unused = Vec::new();
        for file in files {
            if !used.query_row([&file], |r| r.get::<_, bool>(0))? {
                unused.push(file);
            }
        }
        Ok(unused)
    }
}

pub enum ImagesOf {
    Image(i64),
    Comment(i64),
    Task(i64),
    Project(i64),
}

pub const IMAGE_TYPES: [(&str, &str); 4] =
    [("png", "image/png"), ("jpg", "image/jpeg"), ("gif", "image/gif"), ("webp", "image/webp")];

pub fn tasks(n: i64) -> &'static str {
    if n == 1 { "task" } else { "tasks" }
}

pub fn valid_image_file(file: &str) -> bool {
    match file.split_once('.') {
        Some((hash, ext)) => {
            hash.len() == 16
                && hash.bytes().all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
                && IMAGE_TYPES.iter().any(|(e, _)| *e == ext)
        }
        None => false,
    }
}

fn check_image(file: &str) -> Result<()> {
    if valid_image_file(file) { Ok(()) } else { Err(Error::Invalid(format!("{file:?} is not an image Kanzo saved"))) }
}

fn valid_slug(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty()
        && b.len() <= 32
        && b[0].is_ascii_alphanumeric()
        && b.iter().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'-')
}

pub fn short_time(ts: &str) -> String {
    match (ts.get(..10), ts.get(11..16)) {
        (Some(d), Some(t)) => format!("{d} {t} UTC"),
        _ => ts.to_string(),
    }
}

const COMMENT_COUNT: &str = "(SELECT count(*) FROM comments WHERE task_id = t.id)";
const IMAGE_COUNT: &str = "(SELECT count(*) FROM images WHERE task_id = t.id)";
const QUALIFIERS: [&str; 15] = [
    "tag",
    "status",
    "project",
    "title",
    "description",
    "url",
    "comment",
    "id",
    "comments",
    "images",
    "created",
    "updated",
    "is",
    "has",
    "no",
];

/// A query as SQL to add to a WHERE clause: every part must match. A part is a word,
/// `"some words"`, or a qualifier from the README's table, which `-` negates. Anything that
/// isn't a complete qualifier, like `http://x` or `created:soon`, is a word.
#[derive(Default)]
struct Filter {
    sql: String,
    values: Vec<String>,
    first: usize,
}

impl Filter {
    fn new(query: &str, first: usize, hidden: &[String]) -> Self {
        let mut filter = Self { first, ..Default::default() };
        for (part, quoted) in query_parts(query).into_iter().take(10) {
            let part = part.to_lowercase();
            let qualifier = if quoted { None } else { filter.qualifier(&part, hidden) };
            let clause = qualifier.unwrap_or_else(|| filter.word(&part, hidden));
            if !clause.is_empty() {
                filter.sql.push_str(&format!(" AND {clause}"));
            }
        }
        filter
    }

    fn bind(&mut self, value: &str) -> String {
        self.values.push(value.to_string());
        format!("?{}", self.first + self.values.len() - 1)
    }

    fn word(&mut self, word: &str, hidden: &[String]) -> String {
        let seen = |property: &str| !hidden.iter().any(|h| h == property);
        let n = self.bind(word);
        let mut text = String::from("p.slug || '#' || t.id || ' ' || t.title");
        if seen("description") {
            text.push_str(" || ' ' || t.description");
        }
        if seen("tags") {
            text.push_str(" || ' ' || t.tags");
        }
        let mut sql = format!("has_word({text}, {n})");
        if seen("comments") {
            sql.push_str(&format!(" OR t.id IN (SELECT task_id FROM comments WHERE has_word(body, {n}))"));
        }
        format!("({sql})")
    }

    /// `None` when `part` is a plain word. Empty while its value is still being typed.
    fn qualifier(&mut self, part: &str, hidden: &[String]) -> Option<String> {
        let (negated, part) = part.strip_prefix('-').map_or((false, part), |rest| (true, rest));
        let (name, value) = part.split_once(':')?;
        let property = match name {
            "tag" => "tags",
            "comment" => "comments",
            "is" => "human",
            "has" | "no" => value,
            other => other,
        };
        if hidden.iter().any(|h| h == property) {
            return None;
        }
        if value.is_empty() && QUALIFIERS.contains(&name) {
            return Some(String::new());
        }
        let sql = match (name, value) {
            ("tag", tag) => format!("has_word(',' || t.tags || ',', {})", self.bind(&format!(",{tag},"))),
            ("status", status) => format!("t.status = {}", self.bind(status)),
            ("project", slug) => format!("p.slug = {}", self.bind(slug)),
            ("title", word) => format!("has_word(t.title, {})", self.bind(word)),
            ("description", word) => format!("has_word(t.description, {})", self.bind(word)),
            ("url", word) => format!("has_word(t.url, {})", self.bind(word)),
            ("comment", word) => {
                format!("t.id IN (SELECT task_id FROM comments WHERE has_word(body, {}))", self.bind(word))
            }
            ("id", range) => range_sql("t.id", range, number)?,
            ("comments", range) => range_sql(COMMENT_COUNT, range, number)?,
            ("images", range) => range_sql(IMAGE_COUNT, range, number)?,
            ("created", range) => range_sql("substr(t.created_at, 1, 10)", range, date)?,
            ("updated", range) => range_sql("substr(t.updated_at, 1, 10)", range, date)?,
            ("is", "human") => "t.human".to_string(),
            ("has" | "no", "description") => "t.description <> ''".to_string(),
            ("has" | "no", "tags") => "t.tags <> ''".to_string(),
            ("has" | "no", "url") => "t.url <> ''".to_string(),
            ("has" | "no", "comments") => format!("{COMMENT_COUNT} > 0"),
            ("has" | "no", "images") => format!("{IMAGE_COUNT} > 0"),
            _ => return None,
        };
        Some(if negated != (name == "no") { format!("NOT ({sql})") } else { sql })
    }
}

/// Splits on spaces outside double quotes, and drops the quotes: `tag:"needs review"` is one
/// part. True when the part starts with a quote, which makes it a word whatever it says.
fn query_parts(query: &str) -> Vec<(String, bool)> {
    let mut parts = Vec::new();
    let (mut part, mut quoted, mut starts_quoted) = (String::new(), false, false);
    for c in query.chars() {
        match c {
            '"' => {
                starts_quoted |= part.is_empty() && !quoted;
                quoted = !quoted;
            }
            c if c.is_whitespace() && !quoted => {
                if !part.is_empty() {
                    parts.push((std::mem::take(&mut part), starts_quoted));
                }
                starts_quoted = false;
            }
            c => part.push(c),
        }
    }
    if !part.is_empty() {
        parts.push((part, starts_quoted));
    }
    parts
}

/// `5`, `>5`, `>=5`, `<5`, `<=5` or `5..9`. `side` only lets through what's safe in SQL.
fn range_sql(column: &str, range: &str, side: fn(&str) -> Option<String>) -> Option<String> {
    if let Some((from, to)) = range.split_once("..") {
        return Some(format!("{column} BETWEEN {} AND {}", side(from)?, side(to)?));
    }
    for op in [">=", "<=", ">", "<"] {
        if let Some(rest) = range.strip_prefix(op) {
            return Some(format!("{column} {op} {}", side(rest)?));
        }
    }
    Some(format!("{column} = {}", side(range)?))
}

fn number(text: &str) -> Option<String> {
    text.parse::<i64>().ok().map(|n| n.to_string())
}

fn date(text: &str) -> Option<String> {
    let dashes = |i: usize, b: u8| if i == 4 || i == 7 { b == b'-' } else { b.is_ascii_digit() };
    (text.len() == 10 && text.bytes().enumerate().all(|(i, b)| dashes(i, b))).then(|| format!("'{text}'"))
}

fn clean_tags(input: &str) -> Vec<String> {
    let mut tags: Vec<String> = Vec::new();
    for tag in input.split(',') {
        let tag = squash(tag).to_lowercase();
        if !tag.is_empty() && !tags.contains(&tag) {
            tags.push(tag);
        }
    }
    tags
}

/// Any site will do, but only http and https: the board makes it a link people click.
fn clean_url(input: &str) -> Result<String> {
    let url = input.trim();
    if url.is_empty() {
        return Ok(String::new());
    }
    let host = url
        .split_once("://")
        .filter(|(scheme, _)| scheme.eq_ignore_ascii_case("http") || scheme.eq_ignore_ascii_case("https"))
        .and_then(|(_, rest)| rest.split(['/', '?', '#']).next());
    if host.is_none_or(str::is_empty) || url.chars().any(char::is_whitespace) {
        return Err(Error::Invalid(format!("URL {url:?} should start with http:// or https://")));
    }
    Ok(url.to_string())
}

fn clean_repo(repo: &str) -> Result<String> {
    repo_from_url(repo).ok_or_else(|| Error::Invalid(format!("repo {:?} should look like owner/repo", repo.trim())))
}

fn squash(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for word in s.split_whitespace() {
        if !out.is_empty() {
            out.push(' ');
        }
        out.push_str(word);
    }
    out
}

/// A comment as one line of plain text for a list: Markdown marks and link targets dropped,
/// the first 200 characters kept.
fn excerpt(body: &str) -> String {
    let mut out = String::with_capacity(body.len().min(400));
    for line in body.lines() {
        let line = line.trim_start().trim_start_matches(['#', '>']).trim_start();
        let line = line.strip_prefix("- ").or_else(|| line.strip_prefix("* ")).unwrap_or(line);
        let mut chars = line.chars().peekable();
        while let Some(c) = chars.next() {
            match c {
                '*' | '`' | '[' => {}
                '!' if chars.peek() == Some(&'[') => {}
                // `[text](target)` keeps the text.
                ']' if chars.peek() == Some(&'(') => chars.by_ref().take_while(|c| *c != ')').for_each(drop),
                _ => out.push(c),
            }
        }
        out.push(' ');
    }
    squash(&out).chars().take(200).collect()
}

fn clean_title(title: &str) -> Result<String> {
    let title = squash(title);
    if title.is_empty() {
        return Err(Error::Invalid("task title is empty".into()));
    }
    Ok(title)
}

fn parse_task_key(key: &str) -> Result<(Option<String>, i64)> {
    let key = key.trim();
    let (slug, num) = match key.rsplit_once('#') {
        Some((s, n)) => (Some(s.trim().to_ascii_lowercase()).filter(|s| !s.is_empty()), n),
        None => (None, key),
    };
    let id = num
        .trim()
        .parse::<i64>()
        .map_err(|_| Error::Invalid(format!("{key:?} is not a task id; use a form like api#12 or 12")))?;
    Ok((slug, id))
}

pub fn repo_from_url(input: &str) -> Option<String> {
    let s = input.trim();
    let (host, path) = if let Some((_, rest)) = s.split_once("://") {
        rest.split_once('/')?
    } else {
        s.split_once(':').unwrap_or(("", s))
    };
    // A repo is its whole path, as GitLab nests them. A pasted page goes past it: on GitLab
    // after /-/, and on GitHub after owner/repo.
    let mut parts: Vec<&str> = path.split("/-/").next()?.split('/').filter(|p| !p.is_empty()).collect();
    if host.to_ascii_lowercase().ends_with("github.com") {
        parts.truncate(2);
    }
    let last = parts.last_mut()?;
    *last = last.strip_suffix(".git").unwrap_or(*last);
    let ok = |p: &str| p.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'));
    (parts.len() >= 2 && parts.iter().all(|p| ok(p))).then(|| parts.join("/").to_ascii_lowercase())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn store() -> (tempfile::TempDir, Store) {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open(&dir.path().join("kanzo.db"), None).unwrap();
        store.migrate().unwrap();
        (dir, store)
    }

    /// The log as "actor action title detail" lines, oldest first.
    fn log(s: &Store) -> Vec<String> {
        let mut rows = s.list_activity(None, None).unwrap();
        rows.reverse();
        rows.iter()
            .map(|a| format!("{} {} {} [{}]", a.actor.as_deref().unwrap_or("-"), a.action, a.title, a.detail))
            .collect()
    }

    #[test]
    fn the_activity_log_records_every_change_and_who_made_it() {
        let (_dir, s) = store();
        let s = s.acting("board");
        let p = s.create_project("api", "API", None).unwrap();
        let t = s.create_task(p.id, "Ship it", "").unwrap();
        s.move_task(t.id, "doing", None).unwrap();
        s.move_task(t.id, "doing", None).unwrap(); // the same column: not a move
        s.update_task(
            t.id,
            TaskPatch { title: Some("Ship it now".into()), tags: Some("ops".into()), ..Default::default() },
        )
        .unwrap();
        s.update_task(t.id, TaskPatch { status: Some("review".into()), ..Default::default() }).unwrap();
        let c = s.add_comment(t.id, "me", "done, see the PR", &[]).unwrap();
        s.delete_comment(c.id).unwrap();
        s.delete_task(t.id).unwrap();
        assert_eq!(
            log(&s),
            [
                "board project created API []",
                "board created Ship it [todo]",
                "board moved Ship it [todo > doing]",
                "board edited Ship it now [title (was: Ship it), tags ( > ops)]",
                "board moved Ship it now [doing > review]",
                "board commented Ship it now [done, see the PR]",
                "board comment deleted Ship it now [done, see the PR]",
                "board deleted Ship it now [review]",
            ]
        );
        let rows = s.list_activity(None, Some("api")).unwrap();
        assert_eq!(rows.len(), 8);
        assert_eq!((rows[0].task_id, rows[0].project.as_deref()), (Some(t.id), Some("api")));
        assert_eq!(s.list_activity(Some(rows[6].id), None).unwrap().len(), 1, "the page before a row");
        assert!(s.list_activity(None, Some("web")).unwrap().is_empty());
    }

    #[test]
    fn deleting_a_project_logs_its_tasks_under_its_name() {
        let (_dir, s) = store();
        let s = s.acting("claude-code (api)");
        let p = s.create_project("api", "API", None).unwrap();
        let t = s.create_task(p.id, "One", "").unwrap();
        s.add_comment(t.id, "me", "note", &[]).unwrap();
        s.create_task(p.id, "Two", "").unwrap();
        s.delete_project("api").unwrap();
        let rows = s.list_activity(None, None).unwrap();
        let tail: Vec<_> =
            rows.iter().take(3).map(|a| (a.action.as_str(), a.project.as_deref(), a.title.as_str())).collect();
        assert_eq!(tail[0], ("project deleted", Some("api"), "API"));
        assert!(tail[1..].iter().all(|r| r.0 == "deleted" && r.1 == Some("api")), "{tail:?}");
        assert!(rows.iter().all(|a| a.actor.as_deref() == Some("claude-code (api)")));
        assert_eq!(
            rows.iter().filter(|a| a.action == "comment deleted").count(),
            0,
            "the task's row covers its comments"
        );
    }

    /// A board from before the log gets what it already knows, in order; a writer that is
    /// not this Kanzo is still recorded, unsigned.
    #[test]
    fn the_activity_log_starts_from_what_the_board_knows() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("kanzo.db");
        let s = Store::open(&path, None).unwrap();
        s.migrate().unwrap();
        let p = s.create_project("api", "API", None).unwrap();
        let t = s.create_task(p.id, "Old task", "").unwrap();
        s.add_comment(t.id, "claude-code", "from before", &[]).unwrap();
        s.conn.execute_batch("DELETE FROM activity; DELETE FROM meta WHERE key = 'activity';").unwrap();
        drop(s);
        let s = Store::open(&path, None).unwrap();
        s.migrate().unwrap();
        assert_eq!(log(&s), ["- created Old task []", "claude-code commented Old task [from before]"]);
        s.migrate().unwrap();
        assert_eq!(log(&s).len(), 2, "set up once");
        s.conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?1", [t.id]).unwrap();
        assert_eq!(log(&s)[2], "- moved Old task [todo > done]");
    }

    fn titles(tasks: &[Task]) -> Vec<&str> {
        tasks.iter().map(|t| t.title.as_str()).collect()
    }

    #[test]
    fn migrate_is_idempotent() {
        let (_dir, s) = store();
        s.migrate().unwrap();
        let v: String =
            s.conn.query_row("SELECT value FROM meta WHERE key = 'schema_version'", [], |r| r.get(0)).unwrap();
        assert_eq!(v, MIGRATIONS.len().to_string());
    }

    #[test]
    fn keeps_only_the_newest_done_tasks() {
        let (_dir, s) = store();
        let p = s.create_project("api", "", None).unwrap();
        s.create_task(p.id, "open", "").unwrap();
        for title in ["d1", "d2", "d3", "d4"] {
            let t = s.create_task(p.id, title, "").unwrap();
            s.move_task(t.id, "done", None).unwrap();
        }
        let (tasks, hidden) = s.list_tasks_keeping_done(None, None, Some(2)).unwrap();
        assert_eq!((titles(&tasks), hidden), (vec!["open", "d3", "d4"], 2));
        let (tasks, hidden) = s.list_tasks_keeping_done(Some(p.id), Some("done"), Some(0)).unwrap();
        assert_eq!((tasks.len(), hidden), (0, 4));
        assert_eq!(s.list_tasks_keeping_done(None, Some("todo"), Some(0)).unwrap().1, 0);
        let (all, hidden) = s.list_tasks_keeping_done(None, None, None).unwrap();
        assert_eq!((titles(&all), hidden), (vec!["open", "d1", "d2", "d3", "d4"], 0));
    }

    #[test]
    fn opens_a_board_from_a_newer_kanzo_when_it_can() {
        let (_dir, s) = store();
        let set = |version: usize, compatible: usize| {
            s.conn
                .execute_batch(&format!(
                    "UPDATE meta SET value = '{version}' WHERE key = 'schema_version';
                     UPDATE meta SET value = '{compatible}' WHERE key = 'compatible';"
                ))
                .unwrap();
        };
        let newer = MIGRATIONS.len() + 1;
        set(newer, MIGRATIONS.len());
        s.migrate().unwrap();
        s.create_project("api", "", None).unwrap();
        let v: String =
            s.conn.query_row("SELECT value FROM meta WHERE key = 'schema_version'", [], |r| r.get(0)).unwrap();
        assert_eq!(v, newer.to_string());
        set(newer, newer);
        assert!(s.migrate().unwrap_err().to_string().contains("kanzo update"));
    }

    #[test]
    fn projects() {
        let (_dir, s) = store();
        let p = s.create_project("API", "API server", Some("https://github.com/Me/Api.git")).unwrap();
        assert_eq!((p.slug.as_str(), p.repos.as_slice()), ("api", ["me/api".to_string()].as_slice()));
        assert_eq!(s.find_project("me/api").unwrap().id, p.id);
        assert_eq!(s.project_by_repo("git@github.com:me/api.git").unwrap().unwrap().id, p.id);
        assert!(s.create_project("api", "", None).unwrap_err().to_string().contains("already exists"));
        assert!(s.create_project("web", "", Some("me/api")).unwrap_err().to_string().contains("already linked"));
        assert!(s.create_project("bad slug", "", None).is_err());
        let web = s.create_project("web", "", None).unwrap();
        assert_eq!((web.name.as_str(), web.color.as_deref()), ("web", None));
        assert_eq!(s.set_project_color("web", "teal").unwrap().color.as_deref(), Some("teal"));
        assert!(s.set_project_color("web", "#ff0000").is_err());
        let renamed = s.rename_project("web", "  Web   site ").unwrap();
        assert_eq!((renamed.slug.as_str(), renamed.name.as_str()), ("web", "Web site"));
        assert!(s.rename_project("web", "  ").is_err());
        assert!(s.find_project("nope").unwrap_err().to_string().contains("api, web"));
    }

    #[test]
    fn many_repos_per_project() {
        let (_dir, s) = store();
        let api = s.create_project("api", "", Some("me/api")).unwrap();
        s.create_project("web", "", None).unwrap();
        assert_eq!(s.link_repo("api", "https://github.com/Me/API-docs.git").unwrap().repos, ["me/api", "me/api-docs"]);
        assert_eq!(s.find_project("me/api-docs").unwrap().id, api.id);
        assert_eq!(s.link_repo("api", "me/api").unwrap().repos.len(), 2, "linking twice changes nothing");
        assert!(s.link_repo("web", "me/api-docs").unwrap_err().to_string().contains("already linked to project api"));
        assert!(s.link_repo("api", "not a repo").is_err());
        assert_eq!(s.unlink_repo("api", "me/api").unwrap().repos, ["me/api-docs"]);
        assert!(s.project_by_repo("me/api").unwrap().is_none());
        assert!(s.unlink_repo("api", "me/api").is_err());
        s.delete_project("api").unwrap();
        assert_eq!(s.link_repo("web", "me/api-docs").unwrap().repos, ["me/api-docs"]);
    }

    #[test]
    fn search() {
        let (_dir, s) = store();
        let api = s.create_project("api", "", None).unwrap();
        let web = s.create_project("web", "", None).unwrap();
        let login = s.create_task(api.id, "Fix login", "Users loop on /login").unwrap();
        let old = s.create_task(web.id, "Login page copy", "").unwrap();
        s.update_task(old.id, TaskPatch { status: Some("done".into()), ..Default::default() }).unwrap();
        let other = s.create_task(web.id, "Pricing 100%", "").unwrap();
        s.update_task(other.id, TaskPatch { tags: Some("ui".into()), ..Default::default() }).unwrap();
        let titles = |q: &str| s.search(q, Some(10)).unwrap().into_iter().map(|t| t.title).collect::<Vec<_>>();
        assert_eq!(titles("LOGIN"), ["Fix login", "Login page copy"], "any project, open first, done too");
        assert_eq!(titles("login api"), ["Fix login"], "every word must match, the project too");
        assert_eq!(titles(&format!("web#{}", other.id)), ["Pricing 100%"], "by key");
        assert_eq!(titles("ui"), ["Pricing 100%"], "by tag");
        s.add_comment(old.id, "you", "Needs the new logo", &[]).unwrap();
        assert_eq!(titles("logo login"), ["Login page copy"], "by comment, with the task's own words");
        assert_eq!(titles("100%"), ["Pricing 100%"], "% is a plain character");
        assert!(titles("nothing-like-this").is_empty());
        assert_eq!(s.search("", Some(10)).unwrap().len(), 3, "no words: the latest changes");
        assert_eq!(s.search("login", Some(1)).unwrap()[0].id, login.id);
    }

    #[test]
    fn filter_by_any_property() {
        let (_dir, s) = store();
        let api = s.create_project("api", "", None).unwrap();
        let web = s.create_project("web", "", None).unwrap();
        let patch = |tags: &str, status: &str| TaskPatch {
            tags: Some(tags.into()),
            status: Some(status.into()),
            description: Some("Users loop on /login".into()),
            ..Default::default()
        };
        let login = s.create_task_in(api.id, "Fix login", patch("Bug, needs review", "doing")).unwrap();
        let debug = s.create_task_in(api.id, "Debug tools", patch("debugger", "todo")).unwrap();
        let human = s.create_task(web.id, "Call the Bank", "").unwrap();
        s.update_task(
            human.id,
            TaskPatch { human: Some(true), url: Some("https://x.dev/1".into()), ..Default::default() },
        )
        .unwrap();
        s.add_comment(debug.id, "you", "Needs the new logo", &[]).unwrap();
        s.add_comment(debug.id, "you", "Soon", &[]).unwrap();
        s.conn.execute("INSERT INTO images (task_id, file) VALUES (?1, 'a.png')", [human.id]).unwrap();
        s.conn
            .execute("UPDATE tasks SET created_at = '2026-01-15T10:00:00Z', updated_at = '2026-03-01T10:00:00Z'", [])
            .unwrap();
        s.conn.execute("UPDATE tasks SET created_at = '2026-02-01T10:00:00Z' WHERE id = ?1", [login.id]).unwrap();

        let ids = |q: &str| {
            let mut ids: Vec<i64> = s.search(q, None).unwrap().into_iter().map(|t| t.id).collect();
            ids.sort_unstable();
            ids
        };
        let (l, d, h) = (login.id, debug.id, human.id);
        for (q, want) in [
            ("tag:bug", vec![l]),
            ("TAG:BUG", vec![l]),
            ("tag:debug", vec![]),
            ("tag:\"needs review\"", vec![l]),
            ("-tag:bug", vec![d, h]),
            ("status:doing", vec![l]),
            ("status:DOING", vec![l]),
            ("project:api", vec![l, d]),
            ("-project:api", vec![h]),
            ("title:login", vec![l]),
            ("title:loop", vec![]),
            ("description:loop", vec![l, d]),
            ("url:x.dev", vec![h]),
            ("comment:logo", vec![d]),
            (&format!("id:{l}"), vec![l]),
            (&format!("id:>{l}"), vec![d, h]),
            (&format!("id:{l}..{d}"), vec![l, d]),
            ("comments:2", vec![d]),
            ("comments:>=1", vec![d]),
            ("comments:<1", vec![l, h]),
            ("images:1", vec![h]),
            ("created:2026-02-01", vec![l]),
            ("created:<2026-02-01", vec![d, h]),
            ("created:>=2026-02-01", vec![l]),
            ("created:2026-01-01..2026-01-31", vec![d, h]),
            ("updated:2026-03-01", vec![l, d, h]),
            ("is:human", vec![h]),
            ("-is:human", vec![l, d]),
            ("has:url", vec![h]),
            ("no:url", vec![l, d]),
            ("-has:url", vec![l, d]),
            ("-no:url", vec![h]),
            ("has:tags", vec![l, d]),
            ("has:description", vec![l, d]),
            ("has:comments", vec![d]),
            ("no:images", vec![l, d]),
            ("tag:bug status:doing", vec![l]),
            ("tag:bug status:todo", vec![]),
            ("project:api fix", vec![l]),
            ("project:api -status:doing", vec![d]),
            // Being typed: filters nothing yet.
            ("tag:", vec![l, d, h]),
            ("-status:", vec![l, d, h]),
            ("has:", vec![l, d, h]),
            // Not a complete qualifier: a word.
            ("created:soon", vec![]),
            ("comments:many", vec![]),
            ("is:nothing", vec![]),
            ("has:nothing", vec![]),
            ("http://x.dev", vec![]),
            ("-login", vec![]),
            // Quotes make a phrase, or text that only looks like a qualifier.
            ("\"loop on\"", vec![l, d]),
            ("\"tag:bug\"", vec![]),
        ] {
            assert_eq!(ids(q), want, "{q}");
        }
        s.update_task(l, TaskPatch { description: Some("status:doing is the text".into()), ..Default::default() })
            .unwrap();
        assert_eq!(ids("\"status:doing\""), [l]);
    }

    #[test]
    fn filter_values_cannot_change_the_sql() {
        let (_dir, s) = store();
        let api = s.create_project("api", "", None).unwrap();
        s.create_task(api.id, "Keep me", "").unwrap();
        for q in [
            "tag:x'OR'1'='1",
            "created:2026-01-01'OR'1'='1",
            "created:'OR'1'='1'--",
            "id:1)OR(1=1",
            "comments:1;DROP",
            "created:2026-01-0'",
            "title:'",
            "status:'--",
        ] {
            assert!(s.search(q, None).unwrap().is_empty(), "{q}");
        }
        assert_eq!(s.search("", None).unwrap().len(), 1);
    }

    #[test]
    fn next_task_takes_the_top_match_and_never_a_human_one() {
        let (_dir, s) = store();
        let api = s.create_project("api", "", None).unwrap();
        let tagged = |title: &str, human: bool| {
            let patch = TaskPatch { tags: Some("billing".into()), human: Some(human), ..Default::default() };
            s.create_task_in(api.id, title, patch).unwrap();
        };
        s.create_task(api.id, "Not in the epic", "").unwrap();
        tagged("For the user", true);
        tagged("First in the epic", false);
        tagged("Second in the epic", false);

        let next = |q: &str| s.next_task_matching(Some(api.id), q).unwrap().map(|t| t.title);
        assert_eq!(next("is:human"), None);
        assert_eq!(next("tag:nope"), None);
        assert_eq!(next("tag:billing").as_deref(), Some("First in the epic"));
        assert_eq!(next("TAG:billing second").as_deref(), Some("Second in the epic"));
        assert_eq!(next("tag:billing"), None, "the human one stays");
        assert_eq!(next("  ").as_deref(), Some("Not in the epic"));
    }

    #[test]
    fn agents_cannot_filter_through_hidden_properties() {
        let (_dir, s) = store();
        let api = s.create_project("api", "", None).unwrap();
        let patch = TaskPatch { tags: Some("secret".into()), ..Default::default() };
        let t = s.create_task_in(api.id, "Plain title", patch).unwrap();
        s.add_comment(t.id, "you", "whisper", &[]).unwrap();
        let found = |q: &str| s.search_for_agent(q).unwrap().len();
        assert_eq!((found("tag:secret"), found("secret"), found("comment:whisper"), found("whisper")), (1, 1, 1, 1));

        s.set_property("tags", false).unwrap();
        s.set_property("comments", false).unwrap();
        assert_eq!((found("tag:secret"), found("secret"), found("comment:whisper"), found("whisper")), (0, 0, 0, 0));
        assert_eq!(found("has:tags"), 0);
        assert_eq!(s.next_task_matching(None, "tag:secret").unwrap().map(|t| t.id), None);
        assert_eq!(found("plain"), 1, "what stays visible still matches");
        assert_eq!(s.search("tag:secret", None).unwrap().len(), 1, "the board still sees them");
    }

    #[test]
    fn search_ignores_case_beyond_ascii() {
        let (_dir, s) = store();
        let web = s.create_project("web", "", None).unwrap();
        s.create_task(web.id, "Über café", "").unwrap();
        for q in ["über", "ÜBER", "Über", "café", "CAFÉ", "web#1"] {
            assert_eq!(s.search(q, None).unwrap().len(), 1, "{q}");
        }
    }

    #[test]
    fn images() {
        let (_dir, s) = store();
        let p = s.create_project("api", "", None).unwrap();
        let t = s.create_task(p.id, "Bug", "").unwrap();
        let shot = "0123456789abcdef.png".to_string();
        let on_task = s.add_image(t.id, &shot).unwrap();
        assert!(s.add_image(t.id, "../../etc/passwd").is_err());
        assert!(s.add_image(t.id + 1, &shot).is_err());
        let c = s.add_comment(t.id, "me", "", &["fedcba9876543210.jpg".into(), shot.clone()]).unwrap();
        assert!(s.add_comment(t.id, "me", "x", &["evil.svg".into()]).is_err());
        let all: Vec<_> = s.list_images(t.id).unwrap().into_iter().map(|i| (i.comment_id, i.file)).collect();
        assert_eq!(
            all,
            [(None, shot.clone()), (Some(c.id), "fedcba9876543210.jpg".into()), (Some(c.id), shot.clone())]
        );
        assert_eq!(s.image_files(ImagesOf::Task(t.id)).unwrap(), [shot.clone(), "fedcba9876543210.jpg".into()]);
        assert_eq!(s.image_files(ImagesOf::Project(p.id)).unwrap().len(), 2);
        s.delete_image(on_task.id).unwrap();
        assert!(s.unused_images(vec![shot.clone()]).unwrap().is_empty());
        let files = s.image_files(ImagesOf::Comment(c.id)).unwrap();
        s.delete_comment(c.id).unwrap();
        assert!(s.list_images(t.id).unwrap().is_empty());
        assert_eq!(s.unused_images(files).unwrap(), ["fedcba9876543210.jpg".to_string(), shot.clone()]);
        assert!(valid_image_file("6fec521a01297ab5.webp"));
        assert!(!valid_image_file("6FEC521A01297AB5.png") && !valid_image_file("6fec521a01297ab5.svg"));
    }

    #[test]
    fn migration_moves_repos_to_their_own_table() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("kanzo.db");
        {
            let c = Connection::open(&path).unwrap();
            c.execute_batch("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)").unwrap();
            for sql in &MIGRATIONS[..3] {
                c.execute_batch(sql).unwrap();
            }
            c.execute_batch(
                "INSERT INTO meta VALUES ('schema_version', '3');
                 INSERT INTO projects (slug, name, repo) VALUES ('api', 'api', 'me/api'), ('web', 'web', NULL);",
            )
            .unwrap();
        }
        let s = Store::open(&path, None).unwrap();
        s.migrate().unwrap();
        assert_eq!(s.find_project("api").unwrap().repos, ["me/api"]);
        assert!(s.find_project("web").unwrap().repos.is_empty());
        assert_eq!(s.find_project("me/api").unwrap().slug, "api");
    }

    #[test]
    fn task_lifecycle() {
        let (_dir, s) = store();
        let p = s.create_project("api", "API", None).unwrap();
        let t = s.create_task(p.id, "  Fix   login ", "Steps:\n1. log in\n").unwrap();
        assert_eq!((t.title.as_str(), t.status.as_str(), t.key()), ("Fix login", "todo", format!("api#{}", t.id)));
        assert_eq!(s.find_task(&t.key()).unwrap().id, t.id);
        assert!(s.find_task(&format!("web#{}", t.id)).is_err());

        let t = s
            .update_task(
                t.id,
                TaskPatch { status: Some("review".into()), title: Some("Fix login bug".into()), ..Default::default() },
            )
            .unwrap();
        assert_eq!((t.title.as_str(), t.status.as_str()), ("Fix login bug", "review"));
        assert!(s.update_task(t.id, TaskPatch { status: Some("nope".into()), ..Default::default() }).is_err());
        assert!(s.update_task(t.id, TaskPatch { title: Some("  ".into()), ..Default::default() }).is_err());
        let web = s.create_project("web", "", None).unwrap();
        let moved = s.update_task(t.id, TaskPatch { project_id: Some(web.id), ..Default::default() }).unwrap();
        assert_eq!((moved.key(), moved.status.as_str()), (format!("web#{}", t.id), "review"));
        let t = s.update_task(t.id, TaskPatch { project_id: Some(p.id), ..Default::default() }).unwrap();

        s.add_comment(t.id, "claude-code", "Done, see PR", &[]).unwrap();
        s.add_comment(t.id, "", "thanks", &[]).unwrap();
        let comments = s.list_comments(t.id).unwrap();
        assert_eq!(comments.iter().map(|c| c.author.as_str()).collect::<Vec<_>>(), ["claude-code", "me"]);
        assert!(s.add_comment(t.id, "me", " ", &[]).is_err());
        assert_eq!(s.get_task(t.id).unwrap().comments, 2);
        let tagged =
            s.update_task(t.id, TaskPatch { tags: Some(" Bug, UI,bug ,, needs  review".into()), ..Default::default() });
        assert_eq!(tagged.unwrap().tags, ["bug", "ui", "needs review"]);
        assert!(
            s.update_task(t.id, TaskPatch { tags: Some(" , ".into()), ..Default::default() }).unwrap().tags.is_empty()
        );
        let gone = s.add_comment(t.id, "me", "typo", &[]).unwrap();
        assert_eq!(s.delete_comment(gone.id).unwrap().body, "typo");
        assert!(s.delete_comment(gone.id).is_err());
        assert_eq!(s.get_task(t.id).unwrap().comments, 2);
        assert_eq!(s.find_project("api").unwrap().open, 1);

        s.delete_task(t.id).unwrap();
        assert!(s.get_task(t.id).is_err());
        let orphans: i64 = s.conn.query_row("SELECT count(*) FROM comments", [], |r| r.get(0)).unwrap();
        assert_eq!(orphans, 0);
        // Ids are never reused, so api#12 always means the same task.
        let t2 = s.create_task(p.id, "Another", "").unwrap();
        assert!(t2.id > t.id);
    }

    #[test]
    fn delete_project_removes_its_tasks_and_comments() {
        let (_dir, s) = store();
        let api = s.create_project("api", "", Some("me/api")).unwrap();
        let web = s.create_project("web", "", None).unwrap();
        let t = s.create_task(api.id, "a", "").unwrap();
        s.add_comment(t.id, "me", "hi", &[]).unwrap();
        let kept = s.create_task(web.id, "b", "").unwrap();
        s.add_comment(kept.id, "me", "hello", &[]).unwrap();

        assert_eq!(s.delete_project("api").unwrap().id, api.id);
        assert!(s.find_project("api").is_err());
        assert!(s.get_task(t.id).is_err());
        let count = |table: &str| -> i64 {
            s.conn.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0)).unwrap()
        };
        assert_eq!((count("projects"), count("tasks"), count("comments")), (1, 1, 1));
        assert!(s.delete_project("api").is_err());
        s.create_project("api", "", Some("me/api")).unwrap();
    }

    #[test]
    fn ordering_and_moves() {
        let (_dir, s) = store();
        let p = s.create_project("api", "", None).unwrap();
        let a = s.create_task(p.id, "a", "").unwrap();
        let b = s.create_task(p.id, "b", "").unwrap();
        let c = s.create_task(p.id, "c", "").unwrap();
        assert_eq!(titles(&s.list_tasks(Some(p.id), None).unwrap()), ["a", "b", "c"]);

        s.move_task(c.id, "todo", Some(a.id)).unwrap();
        assert_eq!(titles(&s.list_tasks(None, Some("todo")).unwrap()), ["c", "a", "b"]);
        s.move_task(c.id, "todo", None).unwrap();
        assert_eq!(titles(&s.list_tasks(None, Some("todo")).unwrap()), ["a", "b", "c"]);
        s.move_task(a.id, "doing", None).unwrap();
        s.move_task(b.id, "doing", Some(a.id)).unwrap();
        assert_eq!(titles(&s.list_tasks(None, None).unwrap()), ["c", "b", "a"]);

        // Many inserts at the same spot run out of float room and trigger a renumber.
        let mut moving = [b.id, c.id];
        for _ in 0..80 {
            s.move_task(moving[0], "doing", Some(a.id)).unwrap();
            moving.swap(0, 1);
        }
        assert_eq!(titles(&s.list_tasks(None, Some("doing")).unwrap()).last(), Some(&"a"));
        assert!(s.move_task(a.id, "nope", None).is_err());
    }

    #[test]
    fn next_task_takes_top_todo() {
        let (_dir, s) = store();
        let api = s.create_project("api", "", None).unwrap();
        let web = s.create_project("web", "", None).unwrap();
        s.create_task(web.id, "web first", "").unwrap();
        let changes = s.changes().unwrap();
        let patch = TaskPatch { human: Some(true), tags: Some("Bug, bug".into()), ..Default::default() };
        let human = s.create_task_in(api.id, "Call the bank", patch).unwrap();
        assert!(human.human && human.tags == ["bug"] && s.changes().unwrap() == changes + 1, "all in one write");
        s.create_task(api.id, "api first", "").unwrap();
        s.create_task(api.id, "api second", "").unwrap();

        let t = s.next_task(Some(api.id)).unwrap().unwrap();
        assert_eq!((t.title.as_str(), t.status.as_str()), ("api first", "doing"));
        assert_eq!(s.next_task(None).unwrap().unwrap().title, "web first");
        assert_eq!(s.next_task(Some(api.id)).unwrap().unwrap().title, "api second");
        assert!(s.next_task(Some(api.id)).unwrap().is_none());
        assert_eq!(s.get_task(human.id).unwrap().status, "todo");
        assert!(!s.update_task(human.id, TaskPatch { human: Some(false), ..Default::default() }).unwrap().human);
    }

    #[test]
    fn backlog_is_a_column_agents_skip() {
        let (dir, s) = store();
        assert_eq!(s.statuses(), STATUSES);
        s.set_backlog(true).unwrap();
        let s = Store::open(&dir.path().join("kanzo.db"), None).unwrap();
        assert_eq!(s.statuses(), ["backlog", "todo", "doing", "review", "done"]);
        let api = s.create_project("api", "", None).unwrap();
        let status = |s: &str| TaskPatch { status: Some(s.into()), ..Default::default() };
        let backlog = s.create_task_in(api.id, "later", status("backlog")).unwrap();
        assert_eq!(backlog.status, "backlog");
        assert!(s.create_task_in(api.id, "x", status("nope")).is_err());
        assert_eq!(s.create_task(api.id, "now", "").unwrap().status, "todo");

        let t = s.next_task(None).unwrap().unwrap();
        assert_eq!((t.title.as_str(), t.status.as_str()), ("now", "doing"));
        assert!(s.next_task(None).unwrap().is_none());
        assert_eq!(s.get_task(backlog.id).unwrap().status, "backlog");

        assert!(s.set_backlog(false).is_err());
        s.delete_task(backlog.id).unwrap();
        s.set_backlog(false).unwrap();
    }

    #[test]
    fn theme_is_kept_for_every_browser() {
        let (_tmp, s) = store();
        assert_eq!(s.theme().unwrap(), None);
        let before = s.changes().unwrap();
        s.set_theme("skeletal").unwrap();
        assert_eq!(s.theme().unwrap().as_deref(), Some("skeletal"));
        assert!(s.changes().unwrap() > before, "open boards poll the change counter to follow the switch");
        s.set_theme("kanzo").unwrap();
        assert_eq!(s.theme().unwrap().as_deref(), Some("kanzo"));
    }

    #[test]
    fn hidden_properties_keep_their_data() {
        let (_dir, s) = store();
        assert!(s.hidden_properties().unwrap().is_empty());
        let api = s.create_project("api", "", None).unwrap();
        let t = s.create_task(api.id, "x", "Why").unwrap();
        let before = s.changes().unwrap();

        s.set_property("description", false).unwrap();
        s.set_property("tags", false).unwrap();
        s.set_property("tags", false).unwrap();
        assert_eq!(s.hidden_properties().unwrap(), ["description", "tags"]);
        assert!(s.changes().unwrap() > before, "other open boards redraw");
        assert_eq!(s.get_task(t.id).unwrap().description, "Why");

        s.set_property("description", true).unwrap();
        assert_eq!(s.hidden_properties().unwrap(), ["tags"]);
        for always in ["title", "status", "nope"] {
            assert!(s.set_property(always, false).is_err(), "{always}");
        }
    }

    #[test]
    fn change_counter_moves_on_every_write() {
        let (_dir, s) = store();
        let c0 = s.changes().unwrap();
        let p = s.create_project("api", "", None).unwrap();
        let c1 = s.changes().unwrap();
        let t = s.create_task(p.id, "x", "").unwrap();
        let c2 = s.changes().unwrap();
        s.add_comment(t.id, "me", "hi", &[]).unwrap();
        let c3 = s.changes().unwrap();
        assert!(c0 < c1 && c1 < c2 && c2 < c3);
    }

    #[test]
    fn a_task_links_to_one_url_on_any_site() {
        let (_dir, s) = store();
        let api = s.create_project("api", "", None).unwrap();
        let t = s.create_task(api.id, "Fix login", "").unwrap();
        assert_eq!(t.url, "");
        let set = |url: &str| s.update_task(t.id, TaskPatch { url: Some(url.into()), ..Default::default() });
        for url in [
            "https://github.com/Me/Api/pull/12",
            "https://gitlab.example.com/a/b/-/merge_requests/3?x=1#y",
            "http://intranet",
        ] {
            assert_eq!(set(&format!("  {url} ")).unwrap().url, url);
        }
        for bad in [
            "#12",
            "owner/repo#12",
            "github.com/a/b",
            "javascript:alert(1)",
            "javascript://x",
            "https://",
            "https://a b",
        ] {
            assert!(set(bad).unwrap_err().to_string().contains("should start with"), "{bad}");
        }
        assert_eq!(s.get_task(t.id).unwrap().url, "http://intranet", "a bad URL changes nothing");
        assert_eq!(
            s.update_task(t.id, TaskPatch { title: Some("Fix".into()), ..Default::default() }).unwrap().url,
            "http://intranet"
        );
        assert_eq!(set("").unwrap().url, "");
    }

    #[test]
    fn task_keys() {
        assert_eq!(parse_task_key("api#12").unwrap(), (Some("api".into()), 12));
        assert_eq!(parse_task_key("#12").unwrap(), (None, 12));
        assert_eq!(parse_task_key(" 12 ").unwrap(), (None, 12));
        assert!(parse_task_key("api#x").is_err());
    }

    #[test]
    fn repos() {
        for url in [
            "owner/repo",
            "Owner/Repo",
            "https://github.com/owner/repo",
            "https://github.com/owner/repo.git",
            "git@github.com:owner/repo.git",
            "ssh://git@github.com/owner/repo.git",
            "git@github-work:owner/repo.git",
            "https://github.com/owner/repo/",
            "https://github.com/owner/repo/tree/main",
            "https://github.com/owner/repo/pull/3",
            "https://gitea.example.com/owner/repo.git",
            "git@gitea.example.com:owner/repo.git",
            "ssh://git@gitea.example.com:2222/owner/repo.git",
        ] {
            assert_eq!(repo_from_url(url).as_deref(), Some("owner/repo"), "{url}");
        }
        for url in [
            "acme/backend/api",
            "https://gitlab.com/acme/backend/api",
            "https://gitlab.com/acme/backend/api.git",
            "git@gitlab.com:acme/backend/api.git",
            "ssh://git@gitlab.com/acme/backend/api.git",
            "https://gitlab.com/acme/backend/api/-/merge_requests/3",
        ] {
            assert_eq!(repo_from_url(url).as_deref(), Some("acme/backend/api"), "{url}");
        }
        assert_eq!(repo_from_url("repo"), None);
        assert_eq!(repo_from_url("own er/repo"), None);
    }

    #[test]
    fn repos_nested_in_one_group_are_different_repos() {
        let (_dir, s) = store();
        let api = s.create_project("api", "", Some("https://gitlab.com/acme/backend/api.git")).unwrap();
        let web = s.create_project("web", "", Some("git@gitlab.com:acme/backend/web.git")).unwrap();
        assert_eq!(api.repos, ["acme/backend/api"]);
        assert_eq!(s.project_by_repo("acme/backend/web").unwrap().unwrap().id, web.id);
        assert!(s.create_project("api2", "", Some("acme/backend/api")).is_err(), "still one project per repo");
    }

    /// Sets a column straight in the database, for times a test cannot wait for.
    fn set(s: &Store, sql: &str, id: i64) {
        s.conn.execute(sql, [id]).unwrap();
    }

    #[test]
    fn projects_count_review_and_flagged_cards_and_know_their_last_change() {
        let (_dir, s) = store();
        let api = s.create_project("api", "", None).unwrap();
        s.create_project("empty", "", None).unwrap();
        let review = |title| {
            let t = s.create_task(api.id, title, "").unwrap();
            s.update_task(t.id, TaskPatch { status: Some("review".into()), ..Default::default() }).unwrap()
        };
        let a = review("A");
        review("B");
        let flagged = s.create_task(api.id, "C", "").unwrap();
        s.update_task(flagged.id, TaskPatch { human: Some(true), ..Default::default() }).unwrap();
        let done = s.create_task(api.id, "D", "").unwrap();
        s.update_task(done.id, TaskPatch { human: Some(true), status: Some(DONE.into()), ..Default::default() })
            .unwrap();

        let p = s.project_by_slug("api").unwrap().unwrap();
        assert_eq!((p.open, p.review, p.waiting), (3, 2, 1), "a done card counts in neither");
        assert_eq!(s.project_by_slug("empty").unwrap().unwrap().changed_at, "");

        set(&s, "UPDATE tasks SET updated_at = '2020-01-01T00:00:00Z' WHERE project_id = ?1", api.id);
        let newest = s.get_task(a.id).unwrap();
        assert_eq!(s.project_by_slug("api").unwrap().unwrap().changed_at, newest.updated_at);
        let c = s.add_comment(a.id, "claude-code", "Done", &[]).unwrap();
        let listed = s.list_projects().unwrap();
        assert_eq!(listed.iter().find(|p| p.slug == "api").unwrap().changed_at, c.created_at, "a comment counts");
    }

    #[test]
    fn stars_follow_the_projects() {
        let (_dir, s) = store();
        for slug in ["api", "web", "ops"] {
            s.create_project(slug, "", None).unwrap();
        }
        assert!(s.starred().unwrap().is_empty());
        let before = s.changes().unwrap();
        s.set_starred("web", true).unwrap();
        s.set_starred("api", true).unwrap();
        s.set_starred("web", true).unwrap();
        assert_eq!(s.starred().unwrap(), ["api", "web"], "in the order starred, once each");
        assert!(s.changes().unwrap() > before, "other open boards move the project");
        assert!(matches!(s.set_starred("nope", true), Err(Error::NotFound(_))));

        s.set_starred("api", false).unwrap();
        s.set_starred("ops", true).unwrap();
        s.delete_project("web").unwrap();
        assert_eq!(s.starred().unwrap(), ["ops"], "a deleted project drops out on read");
    }

    #[test]
    fn the_inbox_sorts_cards_by_who_wrote_last() {
        let (_dir, s) = store();
        let api = s.create_project("api", "", None).unwrap();
        let card = |title: &str, status: &str, human, last: &[&str]| {
            let t = s.create_task(api.id, title, "").unwrap();
            for author in last {
                s.add_comment(t.id, author, &format!("From {author}"), &[]).unwrap();
            }
            s.update_task(t.id, TaskPatch { status: Some(status.into()), human: Some(human), ..Default::default() })
                .unwrap()
        };
        let answered = card("Answered", "review", false, &[ME, "claude-code"]);
        let stuck = card("Stuck", "doing", true, &["kanzo-dispatch"]);
        card("Mine last", "review", false, &["claude-code", ME]);
        card("Quiet review", "review", false, &[]);
        card("Flagged", "todo", true, &[]);
        card("Flagged, mine last", "todo", true, &["claude-code", ME]);
        card("Finished", DONE, false, &["claude-code"]);
        card("Closed by me", DONE, false, &["claude-code", ME]);
        let old = card("Finished long ago", DONE, false, &["claude-code"]);
        card("Busy", "doing", false, &["claude-code"]);
        set(&s, "UPDATE tasks SET updated_at = '2020-01-01T00:00:00Z' WHERE id = ?1", old.id);
        // The stuck card's reply is the newest, so it leads.
        set(&s, "UPDATE comments SET created_at = '2020-01-01T00:00:00Z' WHERE task_id = ?1", answered.id);

        let inbox = s.inbox().unwrap();
        let titles = |items: &[InboxItem]| items.iter().map(|i| i.task.title.clone()).collect::<Vec<_>>();
        assert_eq!(titles(&inbox.waiting), ["Stuck", "Answered"]);
        assert_eq!(titles(&inbox.review).len(), 2);
        assert!(titles(&inbox.review).contains(&"Mine last".to_string()));
        assert_eq!(inbox.flagged_count, 2);
        assert_eq!(titles(&inbox.finished), ["Finished"]);
        let last = inbox.waiting[1].last.as_ref().unwrap();
        assert_eq!(
            (last.author.as_str(), last.at.as_str(), last.excerpt.as_str()),
            ("claude-code", "2020-01-01T00:00:00Z", "From claude-code")
        );
        assert_eq!(inbox.waiting[0].task.id, stuck.id);
        let quiet = inbox.review.iter().find(|i| i.task.title == "Quiet review").unwrap();
        assert!(quiet.last.is_none());

        for n in 0..25 {
            card(&format!("Done {n}"), DONE, false, &["claude-code"]);
        }
        assert_eq!(s.inbox().unwrap().finished.len(), 20, "at most 20");
    }

    #[test]
    fn an_excerpt_is_one_plain_line() {
        let body = "## Done\n\n**Shipped** the fix, see [the PR](https://example.com/pr/1) and `make test`.\n\n- one\n* two\n> quoted ![shot](/images/a.png)";
        assert_eq!(excerpt(body), "Done Shipped the fix, see the PR and make test. one two quoted shot");
        assert_eq!(excerpt(&"é".repeat(300)).chars().count(), 200);
        assert_eq!(excerpt("snake_case stays"), "snake_case stays");
    }
}
