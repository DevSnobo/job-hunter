-- Waiting applications without activity for at least :stale_days.
-- Bind :as_of (YYYY-MM-DD) and :stale_days (integer).
SELECT a.id, a.company, a.role, a.submitted_on, a.current_status, a.last_activity_on,
       cast(julianday(:as_of) - julianday(a.last_activity_on) AS INTEGER) AS days_since_activity,
       a.completed_interviews
FROM v_application_status a
WHERE a.current_status IN ('in_progress', 'offer_pending')
  AND julianday(:as_of) - julianday(a.last_activity_on) >= :stale_days
ORDER BY days_since_activity DESC, a.last_activity_on;
