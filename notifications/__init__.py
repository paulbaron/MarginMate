"""« Notifications » (`/notifications/`): push notifications to the members'
phones and computers - reminders on a weekly schedule, and alerts when an
event happens (a returnables slip compared with its reprise, an automatic
gather's result).

The pure parts, which touch no database: `webpush` (RFC 8291 encryption,
RFC 8292 VAPID, the push services' allowlist and the one HTTP call),
`schedule` (weekdays, times, the bar's night and the UTC instants they
give) and `registry` (the events, the skip conditions and the pages a
notification may open). A tenant app: its tables live in each bar's
database.
"""
