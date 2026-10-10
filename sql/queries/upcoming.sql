-- Appointments that still need attention. Bind :as_of (YYYY-MM-DD).
-- Excludes completed/cancelled rounds and applications that are already closed.
SELECT r.application_id, a.company, a.role, a.submitted_on,
       a.current_status AS application_status,
       r.id AS round_id, r.sequence_no, r.phase, r.round_type, r.format,
       r.scheduled_for, r.scheduled_time, r.timezone,
       r.current_status AS round_status,
       CASE WHEN r.scheduled_for < :as_of THEN 1 ELSE 0 END AS past_due
FROM v_round_status r
JOIN v_application_status a ON a.id = r.application_id
WHERE r.scheduled_for IS NOT NULL
  AND r.current_status NOT IN ('completed', 'cancelled')
  AND a.current_status NOT IN ('accepted', 'started', 'rejected', 'withdrawn',
                               'offer_declined', 'position_closed', 'inactive')
ORDER BY r.scheduled_for, r.scheduled_time, a.company;
