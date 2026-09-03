import re
from bamboo_client import BambooClient

c = BambooClient(
    "https://bamboo.dev.e2open.com/rest/api/latest",
    auth_method="basic",
    username="vlalitha",
    password="TheEuphoriaV@123$",
)
tests = c.failing_tests("E2NETS-E2NETV3083-VALTST")
seen = set()
count = 0
for t in tests:
    m = t.get("methodName", "")
    mm = re.search(r"for ([\w.]+) - ", m)
    code = mm.group(1) if mm else "?"
    if code in seen:
        continue
    seen.add(code)
    count += 1
    errs = t.get("errors", {}).get("error", [])
    if isinstance(errs, dict):
        errs = [errs]
    for e in errs[:1]:
        msg = e.get("message", "")
        el = re.search(r"declaration of element '(\w+)'", msg)
        print(code, "->", el.group(1) if el else msg[:150])
    if count > 30:
        break
print("total distinct pip codes among failures:", len(seen))
print("total failing tests:", len(tests))
