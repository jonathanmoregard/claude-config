import json
import sys

data = json.load(open(sys.argv[-1]))
total = sum(r["count"] for r in data)
print(f"total: {total}" if "--pretty" in sys.argv else total)
