#!/usr/bin/env python3
"""
Reads the AI Usage section of a just-merged PR and writes traceability into Azure Boards.

For each AB#<id> work item linked in the PR it will:
  - add AI-* tags to that work item
  - create a child "AI Usage" task (copying the parent's iteration/area path)
  - fill the task with the tools/model, task type, how AI helped, and a link to the PR

DRY RUN: if DRY_RUN is set OR no ADO_PAT is provided, it only PRINTS what it parsed and
what it would do. This lets you test the GitHub side first with zero Azure DevOps setup.

Runs AFTER merge, so a failure here never blocks a merge.
All inputs come from environment variables set by the workflow.
"""
import os
import re
import sys
import html
import base64

import requests

API_VERSION = "7.1"

# PR-template label -> Azure Boards tag (keep in sync with PULL_REQUEST_TEMPLATE.md)
TOOL_TAGS = {
    "Copilot Auto": "AI-COPILOT-AUTO",
    "Lightweight model": "AI-LIGHTWEIGHT",
    "Frontier": "AI-FRONTIER",
    "agent mode": "AI-AGENT",
    "Copilot CLI": "AI-CLI",
    "Copilot code review": "AI-CODEREVIEW",
}
TASKTYPE_TAGS = {
    "Quick": "AI-TASK-QUICK",
    "General coding": "AI-TASK-GENERAL",
    "Visual": "AI-TASK-VISUAL",
    "Deep reasoning": "AI-TASK-DEEPREASONING",
    "Agentic": "AI-TASK-AGENTIC",
}


# ---- parsing (no network) --------------------------------------------------
def find_work_items(text):
    return sorted({int(m.group(1)) for m in re.finditer(r"AB#(\d+)", text or "")})


def checked_items(body):
    return [m.group(1).strip()
            for m in re.finditer(r"^\s*-\s*\[[xX]\]\s*(.+?)\s*$", body or "", re.MULTILINE)]


def section_text(body, header):
    pat = re.compile(r"\*\*" + re.escape(header) + r"\*\*.*?\n(.*?)(?=\n\*\*|\Z)", re.DOTALL)
    m = pat.search(body or "")
    if not m:
        return ""
    text = re.sub(r"<!--.*?-->", "", m.group(1), flags=re.DOTALL)
    text = "\n".join(l for l in text.splitlines() if not re.match(r"\s*-\s*\[", l))
    return text.strip()


def derive(body):
    checked = checked_items(body)
    no_ai = any("No AI used" in c for c in checked)
    tools = [c for c in checked if any(k.lower() in c.lower() for k in TOOL_TAGS)]
    tasktypes = [c for c in checked if any(k.lower() in c.lower() for k in TASKTYPE_TAGS)]
    tags = set()
    if not no_ai:
        for c in checked:
            for k, tag in {**TOOL_TAGS, **TASKTYPE_TAGS}.items():
                if k.lower() in c.lower():
                    tags.add(tag)
        tags.add("AI-CODEREVIEW")  # Copilot reviews every PR into main by policy
    return sorted(tags), sorted(tools), sorted(tasktypes), no_ai


def build_description(pr, how, effort, tools, tasktypes):
    rows = []
    if tools:
        rows.append("<li><b>Tools/models:</b> " + html.escape(", ".join(tools)) + "</li>")
    if tasktypes:
        rows.append("<li><b>Task type:</b> " + html.escape(", ".join(tasktypes)) + "</li>")
    if how:
        rows.append("<li><b>How AI helped:</b> " + html.escape(how) + "</li>")
    if effort:
        rows.append("<li><b>Est. effort saved:</b> " + html.escape(effort) + "</li>")
    rows.append(f'<li><b>PR:</b> <a href="{html.escape(pr["url"])}">#{html.escape(pr["number"])}</a> '
                f'by {html.escape(pr["author"])}</li>')
    return "<ul>" + "".join(rows) + "</ul>"


# ---- Azure DevOps ----------------------------------------------------------
class Ado:
    def __init__(self, org_url, project, pat):
        self.base = f"{org_url.rstrip('/')}/{project}/_apis/wit"
        self.project = project
        token = base64.b64encode(f":{pat}".encode()).decode()
        self.h_json = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
        self.h_patch = {"Authorization": f"Basic {token}", "Content-Type": "application/json-patch+json"}

    def get(self, wid):
        r = requests.get(f"{self.base}/workitems/{wid}?api-version={API_VERSION}", headers=self.h_json, timeout=30)
        r.raise_for_status()
        return r.json()

    def merge_tags(self, wid, new_tags):
        item = self.get(wid)
        existing = {t.strip() for t in item["fields"].get("System.Tags", "").split(";") if t.strip()}
        merged = existing | set(new_tags)
        if merged == existing:
            return
        patch = [{"op": "add", "path": "/fields/System.Tags", "value": "; ".join(sorted(merged))}]
        r = requests.patch(f"{self.base}/workitems/{wid}?api-version={API_VERSION}", headers=self.h_patch, json=patch, timeout=30)
        r.raise_for_status()

    def create_ai_usage_task(self, parent, title, description, tags):
        f = parent["fields"]
        patch = [
            {"op": "add", "path": "/fields/System.Title", "value": title[:255]},
            {"op": "add", "path": "/fields/System.Description", "value": description},
            {"op": "add", "path": "/fields/System.IterationPath", "value": f.get("System.IterationPath", self.project)},
            {"op": "add", "path": "/fields/System.AreaPath", "value": f.get("System.AreaPath", self.project)},
            {"op": "add", "path": "/fields/System.Tags", "value": "; ".join(sorted(tags))},
            {"op": "add", "path": "/relations/-", "value": {"rel": "System.LinkTypes.Hierarchy-Reverse", "url": parent["url"]}},
        ]
        r = requests.post(f"{self.base}/workitems/$Task?api-version={API_VERSION}", headers=self.h_patch, json=patch, timeout=30)
        r.raise_for_status()
        return r.json()["id"]


# ---- main ------------------------------------------------------------------
def main():
    pr = {
        "number": os.environ.get("PR_NUMBER", ""),
        "title": os.environ.get("PR_TITLE", ""),
        "body": os.environ.get("PR_BODY") or "",
        "url": os.environ.get("PR_URL", ""),
        "author": os.environ.get("PR_AUTHOR", ""),
    }
    tags, tools, tasktypes, no_ai = derive(pr["body"])
    how = section_text(pr["body"], "How AI helped")
    effort = section_text(pr["body"], "Estimated effort saved (optional)")
    work_items = find_work_items(f"{pr['title']}\n{pr['body']}")

    print("=== Parsed PR ===")
    print("  work items :", work_items or "none")
    print("  tags       :", tags or "none")
    print("  tools      :", tools or "none")
    print("  task type  :", tasktypes or "none")
    print("  how helped :", (how or "(blank)"))
    print("  no AI used :", no_ai)

    if no_ai:
        print("-> 'No AI used' checked; nothing to track."); return
    if not work_items:
        print("-> No AB#<id> link in the PR; nothing to write to Boards."); return

    dry_run = bool(os.environ.get("DRY_RUN")) or not os.environ.get("ADO_PAT")
    if dry_run:
        print(f"-> DRY RUN: would tag {work_items} with {tags} and create an 'AI Usage' task under each.")
        print("   (Set the ADO_PAT secret and remove the DRY_RUN variable to do it for real.)")
        return

    ado = Ado(os.environ["ADO_ORG_URL"], os.environ["ADO_PROJECT"], os.environ["ADO_PAT"])
    title = f"AI Usage — PR #{pr['number']}: {pr['title']}"
    description = build_description(pr, how, effort, tools, tasktypes)

    failures = 0
    for wid in work_items:
        try:
            parent = ado.get(wid)
            ado.merge_tags(wid, tags)
            task_id = ado.create_ai_usage_task(parent, title, description, tags)
            print(f"AB#{wid}: tagged + created AI Usage task #{task_id}")
        except requests.HTTPError as e:
            failures += 1
            body = e.response.text[:300] if e.response is not None else ""
            print(f"AB#{wid}: ERROR {getattr(e.response, 'status_code', '?')} {body}", file=sys.stderr)
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
