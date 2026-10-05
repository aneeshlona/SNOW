import os
import requests
from openpyxl import Workbook
from dotenv import load_dotenv

load_dotenv()

SNOW_URL = os.getenv("SNOW_URL")
SNOW_USERNAME = os.getenv("SNOW_USERNAME")
SNOW_PASSWORD = os.getenv("SNOW_PASSWORD")

url = f"{SNOW_URL}/api/now/table/incident"

params = {
    "sysparm_query": "assignment_groupISEMPTY",
    "sysparm_fields": "sys_id,number,short_description,assignment_group,state,sys_created_on",
    "sysparm_limit": 10000
}

response = requests.get(
    url,
    auth=(SNOW_USERNAME, SNOW_PASSWORD),
    params=params,
    headers={
        "Accept": "application/json"
    }
)

response.raise_for_status()

data = response.json()
incidents = data.get("result", [])

print(f"Found {len(incidents)} incidents with empty assignment group.")

# Create Excel
wb = Workbook()
ws = wb.active
ws.title = "Incidents"

# SAME COLUMN NAMES AS YOUR PREVIOUS EXCEL
ws.append([
    "Number",
    "Short Description",
    "Assignment Group",
    "State",
    "Created",
    "Sys ID"
])

# Add incidents
for inc in incidents:
    ws.append([
        inc.get("number", ""),
        inc.get("short_description", ""),
        inc.get("assignment_group", ""),
        inc.get("state", ""),
        inc.get("sys_created_on", ""),
        inc.get("sys_id", "")
    ])

output_file = "incidents_empty_assignment_group.xlsx"

wb.save(output_file)

print(f"Excel created successfully: {output_file}")
print(f"Total incidents exported: {len(incidents)}")