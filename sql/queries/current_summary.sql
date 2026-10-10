-- One row per current outcome, with interview counts; no personal data.
SELECT current_status, count(*) AS applications,
       sum(completed_interviews) AS interviews_completed,
       round(avg(completed_interviews), 2) AS interviews_per_application
FROM v_application_status
GROUP BY current_status
ORDER BY applications DESC, current_status;
