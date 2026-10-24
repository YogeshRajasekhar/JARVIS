"""Slack and GitHub providers: interface, mock, real REST client."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx


# ============================================================================ Slack
@dataclass
class SlackMessage:
    channel: str
    user: str
    text: str
    ts: datetime


class SlackProvider(ABC):
    @abstractmethod
    def read(self, channel: Optional[str] = None, limit: int = 10) -> list[SlackMessage]: ...

    @abstractmethod
    def post(self, channel_or_user: str, text: str) -> str: ...


class MockSlack(SlackProvider):
    def __init__(self):
        self.messages: list[SlackMessage] = []
        self.posted: list[dict] = []
        self.channels = {"general", "lab-general", "jarvis-dev", "random"}

    def read(self, channel=None, limit=10):
        items = [m for m in self.messages if channel is None or m.channel == channel.lstrip("#")]
        return sorted(items, key=lambda m: m.ts, reverse=True)[:limit]

    def post(self, channel_or_user, text):
        mid = uuid.uuid4().hex[:8]
        self.posted.append({"id": mid, "to": channel_or_user, "text": text})
        return mid


class SlackWebAPI(SlackProvider):
    """Slack Web API with a user token (xoxp-) scoped: channels:history, channels:read, chat:write, users:read, im:history."""

    def __init__(self, token: str):
        self.c = httpx.Client(base_url="https://slack.com/api", headers={"Authorization": f"Bearer {token}"}, timeout=20)
        self._ids: dict[str, str] = {}

    def _call(self, method: str, **params) -> dict:
        r = self.c.post(method, data=params)
        j = r.json()
        if not j.get("ok"):
            raise RuntimeError(f"Slack {method}: {j.get('error')}")
        return j

    def _channel_id(self, name: str) -> str:
        name = name.lstrip("#")
        if name.startswith(("C", "D", "G")) and name.isupper():
            return name
        if not self._ids:
            for ch in self._call("conversations.list", limit=500, types="public_channel,private_channel").get("channels", []):
                self._ids[ch["name"]] = ch["id"]
        if name not in self._ids:
            raise RuntimeError(f"Slack channel #{name} not found")
        return self._ids[name]

    def read(self, channel=None, limit=10):
        cid = self._channel_id(channel or "general")
        msgs = self._call("conversations.history", channel=cid, limit=limit).get("messages", [])
        return [SlackMessage(channel or "general", m.get("user", "bot"), m.get("text", ""),
                             datetime.fromtimestamp(float(m["ts"]), tz=timezone.utc)) for m in msgs]

    def post(self, channel_or_user, text):
        return self._call("chat.postMessage", channel=self._channel_id(channel_or_user), text=text)["ts"]


# ============================================================================ GitHub
class GitHubProvider(ABC):
    default_owner: str

    @abstractmethod
    def notifications(self) -> list[dict]: ...

    @abstractmethod
    def list_prs(self, repo: str) -> list[dict]: ...

    @abstractmethod
    def list_issues(self, repo: str) -> list[dict]: ...

    @abstractmethod
    def ci_status(self, repo: str) -> dict: ...

    @abstractmethod
    def repo_summary(self, repo: str) -> dict: ...

    @abstractmethod
    def create_issue(self, repo: str, title: str, body: str) -> dict: ...

    @abstractmethod
    def comment(self, repo: str, number: int, body: str) -> dict: ...

    def full_name(self, repo: Optional[str]) -> str:
        repo = (repo or "JARVIS").strip().strip("/")
        return repo if "/" in repo else f"{self.default_owner}/{repo}"


class MockGitHub(GitHubProvider):
    def __init__(self, owner: str = "YogeshRajasekhar"):
        self.default_owner = owner
        self.issues: list[dict] = [{"number": 7, "title": "Wake word misfires on TV audio", "state": "open"}]
        self.prs: list[dict] = [{"number": 12, "title": "Add Slack integration", "state": "open", "draft": False}]
        self.notifs: list[dict] = [{"repo": f"{owner}/JARVIS", "title": "Add Slack integration", "type": "PullRequest", "reason": "review_requested"}]
        self.ci = {"state": "success", "workflow": "tests", "branch": "main"}
        self.created: list[dict] = []

    def notifications(self):
        return list(self.notifs)

    def list_prs(self, repo):
        return [p for p in self.prs if p["state"] == "open"]

    def list_issues(self, repo):
        return [i for i in self.issues if i["state"] == "open"]

    def ci_status(self, repo):
        return dict(self.ci, repo=self.full_name(repo))

    def repo_summary(self, repo):
        return {"repo": self.full_name(repo), "open_issues": len(self.list_issues(repo)),
                "open_prs": len(self.list_prs(repo)), "default_branch": "main", "last_push": "today"}

    def create_issue(self, repo, title, body):
        n = 100 + len(self.created)
        item = {"number": n, "title": title, "body": body, "state": "open", "repo": self.full_name(repo)}
        self.created.append(item)
        self.issues.append(item)
        return item

    def comment(self, repo, number, body):
        item = {"issue": number, "body": body, "repo": self.full_name(repo)}
        self.created.append(item)
        return item


class GitHubREST(GitHubProvider):
    """GitHub REST v3. Fine-grained token: read on Issues/PRs/Actions/Notifications, write on Issues only."""

    def __init__(self, token: str, owner: str):
        self.default_owner = owner
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "jarvis-assistant"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.c = httpx.Client(base_url="https://api.github.com", headers=headers, timeout=20)

    def _get(self, path, **params):
        r = self.c.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def notifications(self):
        return [{"repo": n["repository"]["full_name"], "title": n["subject"]["title"],
                 "type": n["subject"]["type"], "reason": n["reason"]} for n in self._get("/notifications")[:15]]

    def list_prs(self, repo):
        return [{"number": p["number"], "title": p["title"], "state": p["state"], "draft": p.get("draft", False)}
                for p in self._get(f"/repos/{self.full_name(repo)}/pulls", state="open")[:15]]

    def list_issues(self, repo):
        return [{"number": i["number"], "title": i["title"], "state": i["state"]}
                for i in self._get(f"/repos/{self.full_name(repo)}/issues", state="open")[:20] if "pull_request" not in i]

    def ci_status(self, repo):
        runs = self._get(f"/repos/{self.full_name(repo)}/actions/runs", per_page=1).get("workflow_runs", [])
        if not runs:
            return {"repo": self.full_name(repo), "state": "none"}
        r = runs[0]
        return {"repo": self.full_name(repo), "state": r.get("conclusion") or r.get("status"),
                "workflow": r.get("name"), "branch": r.get("head_branch")}

    def repo_summary(self, repo):
        j = self._get(f"/repos/{self.full_name(repo)}")
        return {"repo": j["full_name"], "open_issues": j["open_issues_count"], "default_branch": j["default_branch"],
                "last_push": j["pushed_at"], "stars": j["stargazers_count"]}

    def create_issue(self, repo, title, body):
        r = self.c.post(f"/repos/{self.full_name(repo)}/issues", json={"title": title, "body": body or ""})
        r.raise_for_status()
        j = r.json()
        return {"number": j["number"], "title": j["title"], "url": j["html_url"]}

    def comment(self, repo, number, body):
        r = self.c.post(f"/repos/{self.full_name(repo)}/issues/{number}/comments", json={"body": body})
        r.raise_for_status()
        return {"issue": number, "url": r.json()["html_url"]}
