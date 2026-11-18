-- Version: v1 · created 2026-11-16 11:35 EST · updated 2026-11-16 11:35 EST
-- Changes: v1 activity log table and triggers.
-- This file: /home/claude/dev/kanzo/.claude/worktrees/warp-splash/src/activity.sql
-- Session: 1434c01e-24f9-406f-856d-7015b5dd7b69 · vps as claude · cwd /home/claude/dev · slug unified-imagining-globe
-- Resume: ssh -t blenko-claude 'cd /home/claude/dev && claude --resume 1434c01e-24f9-406f-856d-7015b5dd7b69'   (from the Mac)
-- The activity log: one row for every task created, moved, edited or deleted, every comment
-- added or deleted, and every project created or deleted. Triggers write the rows, so any
-- writer is recorded: the board, an agent over MCP, another Kanzo version, a script.
-- `actor` is filled in by Kanzo right after the write, in the same transaction (store.rs);
-- a writer that is not this Kanzo leaves it NULL. Task ids, slugs and titles are copied, so
-- a row still reads right after its task or project is gone.
--
-- Kept out of the numbered migrations in store.rs on purpose: this fork can then take
-- upstream's migrations without the two numbering each other's steps. Every statement can
-- run again; store.rs runs the file once per ACTIVITY_VERSION.
CREATE TABLE IF NOT EXISTS activity (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    actor   TEXT,
    action  TEXT NOT NULL,
    task_id INTEGER,
    project TEXT,
    title   TEXT NOT NULL DEFAULT '',
    detail  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS activity_by_project ON activity(project, id);

-- What the board already knows from before the log existed: when each task was made and
-- each comment written. Moves and edits from then were never recorded.
INSERT INTO activity (at, actor, action, task_id, project, title, detail)
SELECT at, actor, action, task_id, project, title, detail FROM (
    SELECT t.created_at AS at, NULL AS actor, 'created' AS action, t.id AS task_id, p.slug AS project,
           t.title AS title, '' AS detail
    FROM tasks t JOIN projects p ON p.id = t.project_id
    UNION ALL
    SELECT c.created_at, c.author, 'commented', t.id, p.slug, t.title, substr(c.body, 1, 200)
    FROM comments c JOIN tasks t ON t.id = c.task_id JOIN projects p ON p.id = t.project_id
)
WHERE NOT EXISTS (SELECT 1 FROM activity)
ORDER BY at;

DROP TRIGGER IF EXISTS activity_task_insert;
CREATE TRIGGER activity_task_insert AFTER INSERT ON tasks BEGIN
    INSERT INTO activity (action, task_id, project, title, detail)
    VALUES ('created', NEW.id, (SELECT slug FROM projects WHERE id = NEW.project_id), NEW.title, NEW.status);
END;

DROP TRIGGER IF EXISTS activity_task_move;
CREATE TRIGGER activity_task_move AFTER UPDATE OF status ON tasks WHEN OLD.status <> NEW.status BEGIN
    INSERT INTO activity (action, task_id, project, title, detail)
    VALUES ('moved', NEW.id, (SELECT slug FROM projects WHERE id = NEW.project_id), NEW.title,
            OLD.status || ' > ' || NEW.status);
END;

-- A change of position inside a column is not an edit and is not recorded.
DROP TRIGGER IF EXISTS activity_task_edit;
CREATE TRIGGER activity_task_edit AFTER UPDATE ON tasks
WHEN OLD.title <> NEW.title OR OLD.description <> NEW.description OR OLD.tags <> NEW.tags
  OR OLD.human <> NEW.human OR OLD.url <> NEW.url OR OLD.project_id <> NEW.project_id BEGIN
    INSERT INTO activity (action, task_id, project, title, detail)
    VALUES ('edited', NEW.id, (SELECT slug FROM projects WHERE id = NEW.project_id), NEW.title, rtrim(
        CASE WHEN OLD.title <> NEW.title THEN 'title (was: ' || OLD.title || '), ' ELSE '' END ||
        CASE WHEN OLD.description <> NEW.description THEN 'description, ' ELSE '' END ||
        CASE WHEN OLD.tags <> NEW.tags THEN 'tags (' || OLD.tags || ' > ' || NEW.tags || '), ' ELSE '' END ||
        CASE WHEN OLD.human <> NEW.human THEN CASE WHEN NEW.human THEN 'for the human, ' ELSE 'for agents, ' END ELSE '' END ||
        CASE WHEN OLD.url <> NEW.url THEN 'link, ' ELSE '' END ||
        CASE WHEN OLD.project_id <> NEW.project_id
             THEN 'project (was: ' || coalesce((SELECT slug FROM projects WHERE id = OLD.project_id), '?') || '), '
             ELSE '' END, ', '));
END;

DROP TRIGGER IF EXISTS activity_task_delete;
CREATE TRIGGER activity_task_delete AFTER DELETE ON tasks BEGIN
    INSERT INTO activity (action, task_id, project, title, detail)
    VALUES ('deleted', OLD.id, (SELECT slug FROM projects WHERE id = OLD.project_id), OLD.title, OLD.status);
END;

DROP TRIGGER IF EXISTS activity_comment_insert;
CREATE TRIGGER activity_comment_insert AFTER INSERT ON comments BEGIN
    INSERT INTO activity (action, task_id, project, title, detail)
    SELECT 'commented', t.id, p.slug, t.title, substr(NEW.body, 1, 200)
    FROM tasks t JOIN projects p ON p.id = t.project_id WHERE t.id = NEW.task_id;
END;

-- Only a comment deleted on its own: when its task goes, the task's row says so.
DROP TRIGGER IF EXISTS activity_comment_delete;
CREATE TRIGGER activity_comment_delete AFTER DELETE ON comments BEGIN
    INSERT INTO activity (action, task_id, project, title, detail)
    SELECT 'comment deleted', t.id, p.slug, t.title, substr(OLD.body, 1, 200)
    FROM tasks t JOIN projects p ON p.id = t.project_id WHERE t.id = OLD.task_id;
END;

DROP TRIGGER IF EXISTS activity_project_insert;
CREATE TRIGGER activity_project_insert AFTER INSERT ON projects BEGIN
    INSERT INTO activity (action, project, title) VALUES ('project created', NEW.slug, NEW.name);
END;

DROP TRIGGER IF EXISTS activity_project_delete;
CREATE TRIGGER activity_project_delete AFTER DELETE ON projects BEGIN
    INSERT INTO activity (action, project, title) VALUES ('project deleted', OLD.slug, OLD.name);
END;
