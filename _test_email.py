import watcher

ITEMS = [
    {"line": "Gates Foundation AI Fellows Program 2026", "deadline": "2025-09-20"},
    {"line": "APSA Public Scholarship Program 2025–26", "deadline": "2025-09-20"},
    {"line": "Nokia Foundation Scholarship 2025", "deadline": "2025-09-18"},
    {"line": "Infocepts Innovate for Impact Scholarship 2025-26", "deadline": "2025-09-18"},
    {"line": "Maharaja Kirit Bikram Manikya Scholarship 2025", "deadline": "2025-09-17"},
    {"line": "International Internship Pilot Programme", "deadline": "2025-09-15"},
]

text, html = watcher.format_scholarship_table(ITEMS)
sent = watcher.send_email("[Watcher] Scholarship email format test", text, html)
print("Test email sent." if sent else "Test email not sent: configure sender credentials.")
raise SystemExit(0 if sent else 1)
