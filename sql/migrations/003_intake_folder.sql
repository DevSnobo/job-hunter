-- Which intake folder below applications/ an application was registered from.
--
-- The folder name is the application identifier that the posting capture tool
-- (tooling/posting_entry) and the CV batch tool (tooling/cv_batch) use:
-- <company>[_<posting-id>]. The folder itself stays on disk exactly as it is;
-- this column only records where an application came from, so `jobs show` and
-- `jobs intake` can link the two. NULL means the application was entered by hand.
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

ALTER TABLE applications ADD COLUMN intake_folder TEXT CHECK (
    intake_folder IS NULL OR (
        length(trim(intake_folder)) > 0
        AND intake_folder NOT IN ('.', '..')
        AND substr(intake_folder, 1, 1) != '.'
        AND instr(intake_folder, '/') = 0
        AND instr(intake_folder, char(92)) = 0
    )
);

INSERT INTO schema_migrations(version, name) VALUES (3, 'intake_folder');
PRAGMA user_version = 3;
COMMIT;
