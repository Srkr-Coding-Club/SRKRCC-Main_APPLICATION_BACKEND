import csv
import os
import openpyxl

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(BACKEND_ROOT, "sample_backups")
os.makedirs(OUTPUT_DIR, exist_ok=True)

DATASETS = [
    {
        "filename": "01_users_members_directory",
        "domain": "USERS",
        "description": "Student members directory for testing User domain restoration and password setup lifecycle.",
        "headers": [
            "Full Name",
            "Email",
            "Phone Number",
            "Branch",
            "Year of Study",
            "Roll Number",
            "Permanent Club ID",
            "Membership Status",
        ],
        "rows": [
            ["Aarav Sharma", "aarav.sharma@srkr.ac.in", "9876500001", "CSE", 3, "23B91A0501", "25SCC001", "ACTIVE"],
            ["Bhavya Patel", "bhavya.patel@srkr.ac.in", "9876500002", "ECE", 2, "24B91A0402", "25SCC002", "ACTIVE"],
            ["Chaitanya Rao", "chaitanya.rao@srkr.ac.in", "9876500003", "IT", 4, "22B91A1203", "25SCC003", "ACTIVE"],
            ["Divya Sri", "divya.sri@srkr.ac.in", "9876500004", "AIDS", 2, "24B91A5404", "25SCC004", "ACTIVE"],
            ["Eshwar Reddy", "eshwar.reddy@srkr.ac.in", "9876500005", "CSBS", 1, "25B91A4205", "25SCC005", "ACTIVE"],
            ["Farhan Khan", "farhan.khan@srkr.ac.in", "9876500006", "CSE", 3, "23B91A0506", "25SCC006", "ACTIVE"],
            ["Gayatri Varma", "gayatri.varma@srkr.ac.in", "9876500007", "ECE", 3, "23B91A0407", "25SCC007", "ACTIVE"],
            ["Harshavardhan Raju", "harsha.raju@srkr.ac.in", "9876500008", "MECH", 4, "22B91A0308", "25SCC008", "ACTIVE"],
            ["Ishita Sen", "ishita.sen@srkr.ac.in", "9876500009", "CIVIL", 2, "24B91A0109", "25SCC009", "ACTIVE"],
            ["Jagadeesh Kumar", "jagadeesh.kumar@srkr.ac.in", "9876500010", "CSE", 2, "24B91A0510", "25SCC010", "ACTIVE"],
        ],
    },
    {
        "filename": "02_events_schedule",
        "domain": "EVENTS",
        "description": "Club event schedule and workshop sessions for testing the Events domain import.",
        "headers": [
            "Event Name",
            "Event Type",
            "Status",
            "Start Time",
            "End Time",
            "Venue",
            "Description",
            "Registration Fee",
        ],
        "rows": [
            [
                "Full-Stack Web Sprint",
                "WORKSHOP",
                "PUBLISHED",
                "2026-10-15 09:30:00",
                "2026-10-15 17:00:00",
                "CS Lab 4",
                "Hands-on workshop building Next.js 15 App Router web apps and Django REST APIs.",
                "0.00",
            ],
            [
                "AI & Machine Learning BootCamp",
                "BOOTCAMP",
                "PUBLISHED",
                "2026-11-01 10:00:00",
                "2026-11-03 16:30:00",
                "Seminar Hall A",
                "Three-day intensive training in Python data science, PyTorch, and NLP models.",
                "150.00",
            ],
            [
                "Competitive Programming Crash Course",
                "SEMINAR",
                "PUBLISHED",
                "2026-11-20 14:00:00",
                "2026-11-20 18:00:00",
                "Online (Google Meet)",
                "Mastering dynamic programming and graph algorithms for coding placements.",
                "0.00",
            ],
            [
                "Cloud Computing & Docker 101",
                "WORKSHOP",
                "DRAFT",
                "2026-12-05 09:00:00",
                "2026-12-05 13:00:00",
                "Server Room 2",
                "Introduction to containerization, microservices, and AWS EC2 deployments.",
                "50.00",
            ],
            [
                "Alumni Tech Talk: Scalable Backend Systems",
                "TECH_TALK",
                "PUBLISHED",
                "2026-12-18 16:00:00",
                "2026-12-18 18:00:00",
                "Main Auditorium",
                "Senior engineers from top tech firms discuss designing fault-tolerant distributed systems.",
                "0.00",
            ],
        ],
    },
    {
        "filename": "03_hackathons_catalog",
        "domain": "HACKATHONS",
        "description": "Hackathon competition schedule and team parameters for testing the Hackathons domain.",
        "headers": [
            "Hackathon Title",
            "Status",
            "Start Date",
            "End Date",
            "Theme",
            "Min Team Size",
            "Max Team Size",
            "Registration Fee",
        ],
        "rows": [
            [
                "HackOverflow 2026",
                "PUBLISHED",
                "2026-10-24 08:00:00",
                "2026-10-25 20:00:00",
                "AI for Social Good & Smart Campus Automation",
                2,
                4,
                "200.00",
            ],
            [
                "CodeQuest 36-Hour Hackathon",
                "PUBLISHED",
                "2026-11-14 09:00:00",
                "2026-11-15 21:00:00",
                "FinTech, Web3, and Decentralized Identity",
                1,
                3,
                "0.00",
            ],
            [
                "Freshers Ideathon 2026",
                "DRAFT",
                "2026-12-10 10:00:00",
                "2026-12-10 18:00:00",
                "Innovations in Sustainable Engineering & Clean Tech",
                2,
                4,
                "0.00",
            ],
        ],
    },
    {
        "filename": "04_forms_workshop_registrations",
        "domain": "FORMS",
        "description": "Responses and registration data for dynamic forms with custom fields.",
        "headers": [
            "Full Name",
            "Email",
            "Phone Number",
            "Branch",
            "T-Shirt Size",
            "GitHub Profile",
            "Dietary Preference",
            "Prior Experience",
        ],
        "rows": [
            ["Karthik Varma", "karthik.v@srkr.ac.in", "9876511111", "CSE", "L", "https://github.com/karthik-v", "Vegetarian", "Intermediate"],
            ["Lavanya Murthy", "lavanya.m@srkr.ac.in", "9876511112", "IT", "M", "https://github.com/lavanya-m", "Non-Vegetarian", "Beginner"],
            ["Manish Kumar", "manish.k@srkr.ac.in", "9876511113", "ECE", "XL", "https://github.com/manish-k", "Vegetarian", "Advanced"],
            ["Nandini Devi", "nandini.d@srkr.ac.in", "9876511114", "AIDS", "S", "https://github.com/nandini-d", "Vegetarian", "Beginner"],
            ["Omkar Prasad", "omkar.p@srkr.ac.in", "9876511115", "CSBS", "M", "https://github.com/omkar-p", "Non-Vegetarian", "Intermediate"],
        ],
    },
    {
        "filename": "05_unknown_raw_legacy_inventory",
        "domain": "UNKNOWN_RAW",
        "description": "Schemaless legacy dataset (club hardware & lab inventory) to test the Raw Vault escape hatch.",
        "headers": [
            "Asset ID",
            "Item Description",
            "Category",
            "Quantity",
            "Assigned Room",
            "Last Serviced Date",
            "Custodian",
            "Notes",
        ],
        "rows": [
            ["SRKR-HW-001", "Arduino Mega 2560 Development Boards", "Microcontrollers", 15, "Robotics Lab 1", "2026-01-10", "Dr. Prasad", "Stored in Cabinet A"],
            ["SRKR-HW-002", "Raspberry Pi 4 Model B (4GB RAM)", "Single Board Computers", 8, "IoT Innovation Lab", "2026-02-15", "Prof. Sunitha", "Used for edge AI projects"],
            ["SRKR-HW-003", "ESP32 Wi-Fi + BLE Dev Modules", "Wireless SoC", 25, "Robotics Lab 1", "2026-03-01", "Dr. Prasad", "Includes breadboards and jumper wires"],
            ["SRKR-HW-004", "Logitech C920 Pro HD Webcams", "Peripherals", 6, "Seminar Hall A", "2026-04-12", "Srikanth", "Used for hackathon streaming"],
            ["SRKR-HW-005", "SanDisk 64GB High Speed MicroSD Cards", "Storage", 20, "IoT Innovation Lab", "2026-02-15", "Prof. Sunitha", "Flashed with Raspberry Pi OS"],
            ["SRKR-HW-006", "Ender-3 V2 3D Printer", "Rapid Prototyping", 2, "Makerspace", "2026-05-20", "Ramesh Babu", "PLA filament stock in storage rack 3"],
        ],
    },
]


def generate():
    readme_lines = [
        "# Mock & Sample Backup Datasets for SRKR Coding Club",
        "",
        "This folder provides comprehensive, pre-formatted mock datasets in both **`.csv`** and **`.xlsx` (Excel)** formats.",
        "Use these files to test and demonstrate all features of the **Universal Backup Engine**, **Schema Matching Gauge**, and **Password Setup Lifecycle**.",
        "",
        "---",
        "",
        "## Summary of Available Files",
        "",
        "| File | Formats | Target Domain | Purpose / Recommended Workflow |",
        "| :--- | :--- | :--- | :--- |",
        "| `01_users_members_directory` | `.csv`, `.xlsx` | `USERS` | Test member ingestion, permanent Club ID mapping, and password setup link trigger. |",
        "| `02_events_schedule` | `.csv`, `.xlsx` | `EVENTS` | Test event imports with dates, fees, venues, and types. |",
        "| `03_hackathons_catalog` | `.csv`, `.xlsx` | `HACKATHONS` | Test hackathon competitions, team size constraints, and themes. |",
        "| `04_forms_workshop_registrations` | `.csv`, `.xlsx` | `FORMS` | Test dynamic form builder submission ingestion with arbitrary questions. |",
        "| `05_unknown_raw_legacy_inventory` | `.csv`, `.xlsx` | `UNKNOWN_RAW` | Test the **Schemaless Raw Vault** escape hatch (preserves 100% data with zero constraints). |",
        "",
        "---",
        "",
        "## Detailed Dataset Specifications",
        "",
    ]

    for item in DATASETS:
        name = item["filename"]
        headers = item["headers"]
        rows = item["rows"]

        # 1. Write CSV file
        csv_path = os.path.join(OUTPUT_DIR, f"{name}.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerows(rows)
        print(f"Generated CSV:  {csv_path}")

        # 2. Write XLSX file
        xlsx_path = os.path.join(OUTPUT_DIR, f"{name}.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "BackupData"
        ws.append(headers)
        for r in rows:
            ws.append(r)
        wb.save(xlsx_path)
        print(f"Generated XLSX: {xlsx_path}")

        # Add to README
        readme_lines.append(f"### `{name}` (`.csv` & `.xlsx`)")
        readme_lines.append(f"- **Domain**: `{item['domain']}`")
        readme_lines.append(f"- **Description**: {item['description']}")
        readme_lines.append(f"- **Rows**: {len(rows)}")
        readme_lines.append(f"- **Headers**: `{', '.join(headers)}`")
        readme_lines.append("")

    readme_path = os.path.join(OUTPUT_DIR, "README.md")
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write("\n".join(readme_lines))
    print(f"Generated Documentation: {readme_path}")


if __name__ == "__main__":
    generate()
