-- Run only on a disposable database initialized with all migrations.
-- sqlite3 -bail /path/to/test.sqlite3 < sql/tests/schema_smoke.sql
PRAGMA foreign_keys = ON;
BEGIN;
CREATE TEMP TABLE assert_true (label TEXT NOT NULL, ok INTEGER NOT NULL CHECK (ok = 1));
INSERT INTO applications(id, company, role, submitted_on, discovery_source, submission_channel)
VALUES ('test-a', 'Example A', 'Developer', '2026-01-01', 'linkedin', 'company_portal'),
       ('test-b', 'Example B', 'Developer', '2026-01-02', 'instaffo', 'instaffo'),
       ('test-c', 'Example C', 'Developer', '2026-01-03', 'referral', 'email');
INSERT INTO rounds(id, application_id, sequence_no, phase, round_type, format) VALUES
    (101, 'test-a', 1, 'screening', 'recruiter_screen', 'phone'),
    (102, 'test-a', 2, 'interview', 'general', 'video'),
    (103, 'test-a', 3, 'assessment', 'take_home', 'asynchronous'),
    (104, 'test-a', 4, 'interview', 'technical', 'on_site');
INSERT INTO round_participants VALUES (104, 'hiring_manager'), (104, 'team');
INSERT INTO events(application_id, round_id, event_type, occurred_on, day_order) VALUES
    ('test-a', 101, 'invited', '2026-01-02', 1),
    ('test-a', 101, 'completed', '2026-01-03', 1),
    ('test-a', 102, 'invited', '2026-01-03', 2);
INSERT INTO events(application_id, round_id, event_type, occurred_on, day_order,
                   scheduled_for, scheduled_time, timezone) VALUES
    ('test-a', 102, 'scheduled', '2026-01-04', 1, '2026-01-05', '10:00', 'Europe/Berlin'),
    ('test-a', 102, 'rescheduled', '2026-01-04', 2, '2026-01-06', '11:00', 'Europe/Berlin');
INSERT INTO events(application_id, round_id, event_type, occurred_on, day_order) VALUES
    ('test-a', 102, 'completed', '2026-01-06', 1),
    ('test-a', 103, 'assessment_received', '2026-01-06', 2),
    ('test-a', 103, 'assessment_submitted', '2026-01-08', 1),
    ('test-a', 104, 'completed', '2026-01-10', 1),
    ('test-a', NULL, 'rejected', '2026-01-11', 1),
    ('test-a', NULL, 'note', '2026-01-12', 1),
    ('test-b', NULL, 'offer_received', '2026-01-03', 1),
    ('test-b', NULL, 'offer_accepted', '2026-01-03', 2);
INSERT INTO artifacts(application_id, artifact_type, relative_path)
VALUES ('test-a', 'job_posting', 'applications/test-a/posting.txt');

INSERT INTO assert_true VALUES
    ('rejection survives a later note', (SELECT current_status = 'rejected' FROM v_application_status WHERE id = 'test-a')),
    ('screens and assessments are not interviews', (SELECT completed_interviews = 2 AND completed_screens = 1 AND completed_assessments = 1 FROM v_application_status WHERE id = 'test-a')),
    ('latest same-day state wins', (SELECT current_status = 'accepted' FROM v_application_status WHERE id = 'test-b')),
    ('no events means in progress', (SELECT current_status = 'in_progress' FROM v_application_status WHERE id = 'test-c')),
    ('rescheduling preserves latest appointment', (SELECT scheduled_for = '2026-01-06' AND scheduled_time = '11:00' AND current_status = 'completed' FROM v_round_status WHERE id = 102)),
    ('submission appears once in timeline', (SELECT count(*) = 1 FROM v_application_timeline WHERE application_id = 'test-a' AND event_type = 'submitted')),
    ('all foreign keys valid', NOT EXISTS (SELECT 1 FROM pragma_foreign_key_check));
INSERT INTO events(application_id, event_type, occurred_on, day_order)
VALUES ('test-a', 'reopened', '2026-01-13', 1);
INSERT INTO assert_true VALUES
    ('explicit reopening supported', (SELECT current_status = 'in_progress' FROM v_application_status WHERE id = 'test-a'));
SELECT 'PASS: ' || count(*) || ' schema/view assertions' AS result FROM assert_true;
ROLLBACK;
