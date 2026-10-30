import ast
import base64
import ipaddress
import io
import json
import re
import socket
import stat
import time
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlparse

import httpx

from app.github_app import API_URL, api_headers
from app.github_webhooks import redact_text

MAX_FILES = 12
MAX_FILE_BYTES = 20_000
MAX_TOTAL_SOURCE_BYTES = 120_000
MAX_LOG_BYTES = 2_000_000
MAX_ARCHIVE_BYTES = 40_000_000
MAX_EXTRACTED_BYTES = 250_000_000
MAX_ARCHIVE_FILES = 5000
ALLOWED_LOG_HOST_SUFFIXES = (".actions.githubusercontent.com", ".githubusercontent.com")
TEXT_EXTENSIONS = {".py", ".toml", ".cfg", ".ini", ".yml", ".yaml", ".json", ".txt", ".md"}
DEPENDENCY_FILES = {"pyproject.toml", "requirements.txt", "requirements-dev.txt", "Pipfile", "poetry.lock", "uv.lock", "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", ".python-version", ".nvmrc", "runtime.txt", "tox.ini"}
PROTECTED_NAMES = {".env", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "credentials.json", "secrets.yml", "secrets.yaml"}


class GitHubRepositoryError(Exception):
    pass


class GitHubRepositoryClient:
    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self.token = token
        self.transport = transport
        self.timeout = httpx.Timeout(12.0, connect=4.0)

    def _request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> httpx.Response:
        if not path.startswith("/") or path.startswith("//") or ".." in path.split("/"):
            raise GitHubRepositoryError("Invalid GitHub API path")
        with httpx.Client(base_url=API_URL, timeout=self.timeout, follow_redirects=False, transport=self.transport) as client:
            for attempt in range(3):
                try:
                    response = client.request(method, path, params=params, json=json_body, headers=api_headers(self.token))
                except httpx.TimeoutException as exc:
                    if attempt < 2:
                        time.sleep(0.4 * (attempt + 1))
                        continue
                    raise GitHubRepositoryError("GitHub API request timed out") from exc
                except httpx.RequestError as exc:
                    raise GitHubRepositoryError("GitHub API could not be reached") from exc
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        retry_after = response.headers.get("retry-after", "")
                        try:
                            delay = min(2.0, max(0.0, float(retry_after)))
                        except ValueError:
                            delay = 0.4 * (attempt + 1)
                        time.sleep(delay)
                        continue
                return response
        raise GitHubRepositoryError("GitHub API is temporarily unavailable")

    def get_json(self, path: str, *, params: dict | None = None) -> dict:
        response = self._request("GET", path, params=params)
        if response.is_error:
            raise GitHubRepositoryError(f"GitHub API returned HTTP {response.status_code}")
        try:
            result = response.json()
        except ValueError as exc:
            raise GitHubRepositoryError("GitHub returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise GitHubRepositoryError("GitHub returned an invalid API response")
        return result

    def post_json(self, path: str, body: dict) -> dict:
        response = self._request("POST", path, json_body=body)
        if response.is_error:
            raise GitHubRepositoryError(f"GitHub API returned HTTP {response.status_code}")
        try:
            result = response.json()
        except ValueError as exc:
            raise GitHubRepositoryError("GitHub returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise GitHubRepositoryError("GitHub returned an invalid API response")
        return result

    def create_repair_pull_request(
        self,
        *,
        full_name: str,
        base_sha: str,
        base_branch: str,
        repair_id: str,
        changed_paths: list[str],
        worktree: Path,
        failure_summary: str,
        root_cause: str,
        explanation: str,
        validation_checks: list[dict[str, str]],
        validation_environment: dict,
        retry_count: int,
    ) -> dict[str, object]:
        parts = full_name.split("/")
        if len(parts) != 2 or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", part) for part in parts):
            raise GitHubRepositoryError("Repository name is invalid")
        if not re.fullmatch(r"[0-9a-f]{40,64}", base_sha) or not re.fullmatch(r"[0-9a-f-]{36}", repair_id):
            raise GitHubRepositoryError("Repair reference is invalid")
        if not base_branch or len(base_branch) > 255 or any(ord(char) < 32 for char in base_branch):
            raise GitHubRepositoryError("Base branch name is invalid")
        owner, repo = parts
        prefix = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        branch = f"patchwork/repair-{repair_id.replace('-', '')[:24]}"
        existing_pulls = self._request(
            "GET", f"{prefix}/pulls",
            params={"head": f"{owner}:{branch}", "base": base_branch, "state": "all", "per_page": 10},
        )
        if not existing_pulls.is_error:
            try:
                body = existing_pulls.json()
                if isinstance(body, list):
                    for item in body:
                        if (item.get("head") or {}).get("ref") == branch and (item.get("base") or {}).get("ref") == base_branch:
                            head_sha = (item.get("head") or {}).get("sha")
                            if (
                                isinstance(item.get("id"), int) and isinstance(item.get("number"), int)
                                and isinstance(item.get("html_url"), str)
                                and isinstance(head_sha, str) and re.fullmatch(r"[0-9a-f]{40,64}", head_sha)
                            ):
                                _validate_pull_url(item["html_url"])
                                return {"id": item["id"], "number": item["number"], "url": item["html_url"], "branch": branch, "commit_sha": head_sha}
            except (ValueError, AttributeError):
                raise GitHubRepositoryError("GitHub returned an invalid pull request list")
        commit = self.get_json(f"{prefix}/git/commits/{base_sha}")
        base_tree_sha = ((commit.get("tree") or {}).get("sha"))
        if not isinstance(base_tree_sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", base_tree_sha):
            raise GitHubRepositoryError("GitHub returned invalid base commit metadata")
        existing_ref = self._request("GET", f"{prefix}/git/ref/heads/{branch}")
        existing_head_sha: str | None = None
        if existing_ref.status_code == 200:
            try:
                ref_data = existing_ref.json()
                ref_object = ref_data.get("object") if isinstance(ref_data, dict) else None
                candidate_sha = ref_object.get("sha") if isinstance(ref_object, dict) else None
                if (
                    not isinstance(ref_object, dict) or ref_object.get("type") != "commit"
                    or not isinstance(candidate_sha, str)
                    or not re.fullmatch(r"[0-9a-f]{40,64}", candidate_sha)
                ):
                    raise GitHubRepositoryError("GitHub returned invalid repair branch metadata")
                existing_head_sha = candidate_sha
            except ValueError as exc:
                raise GitHubRepositoryError("GitHub returned invalid repair branch metadata") from exc
        elif existing_ref.status_code != 404:
            raise GitHubRepositoryError(f"GitHub API returned HTTP {existing_ref.status_code}")

        tree_entries = []
        for path in changed_paths:
            if not self._safe_source_path(path) or self._extension(path) != ".py":
                raise GitHubRepositoryError("Validated patch contains an unsupported file path")
            file_path = worktree.joinpath(*path.split("/"))
            if not file_path.exists():
                tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                continue
            try:
                contents = file_path.read_bytes()
            except OSError as exc:
                raise GitHubRepositoryError("Validated patch output could not be read") from exc
            if len(contents) > 1_000_000:
                raise GitHubRepositoryError("Validated patch output exceeds the GitHub file limit")
            blob = self.post_json(f"{prefix}/git/blobs", {
                "content": base64.b64encode(contents).decode("ascii"), "encoding": "base64",
            })
            blob_sha = blob.get("sha")
            if not isinstance(blob_sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", blob_sha):
                raise GitHubRepositoryError("GitHub returned invalid blob metadata")
            tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob_sha})
        if not tree_entries:
            raise GitHubRepositoryError("Repair patch does not contain any supported file changes")
        tree = self.post_json(f"{prefix}/git/trees", {"base_tree": base_tree_sha, "tree": tree_entries})
        tree_sha = tree.get("sha")
        if not isinstance(tree_sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", tree_sha):
            raise GitHubRepositoryError("GitHub returned invalid tree metadata")
        new_commit = self.post_json(f"{prefix}/git/commits", {
            "message": f"Propose CI repair {repair_id}", "tree": tree_sha,
            "parents": [existing_head_sha or base_sha],
        })
        commit_sha = new_commit.get("sha")
        if not isinstance(commit_sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit_sha):
            raise GitHubRepositoryError("GitHub returned invalid repair commit metadata")
        if existing_head_sha:
            response = self._request(
                "PATCH", f"{prefix}/git/refs/heads/{branch}",
                json_body={"sha": commit_sha, "force": False},
            )
            if response.is_error:
                raise GitHubRepositoryError(f"GitHub API returned HTTP {response.status_code}")
        else:
            self.post_json(f"{prefix}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit_sha})
        safe_summary = _plain_pr_text(failure_summary, 100)
        title = ("Repair CI failure: " + safe_summary)[:240]
        safe_root_cause = _plain_pr_text(root_cause, 4000)
        safe_explanation = _plain_pr_text(explanation, 4000)
        check_lines = "\n".join(f"- {item['name']}: **{item['outcome']}**" for item in validation_checks[:20]) or "- Validation passed."
        environment_text = json.dumps(validation_environment, sort_keys=True, ensure_ascii=False)[:4000]
        body = (
            "## Automated repair proposal\n\n"
            "This draft was generated by the automated repair system. It is provided for human review and is never merged or deployed automatically.\n\n"
            "### Original failure\n\n"
            f"{safe_summary}\n\n"
            "### Root cause\n\n"
            f"{safe_root_cause}\n\n"
            "### Proposed changes\n\n"
            f"{safe_explanation}\n\n"
            + "\n".join(f"- `{path}`" for path in changed_paths[:10])
            + "\n\n### Validation results\n\n"
            + check_lines
            + f"\n\nValidation environment: `{environment_text}`\n"
            + f"Repair attempts: {retry_count + 1}\n\n"
            + "### Known limitations\n\n"
            + "Passing checks do not prove correctness. Review the patch, dependency assumptions, and test coverage before merging.\n\n"
            + f"Repair job: `{repair_id}`\n\nOriginal commit: `{base_sha}`\n"
        )
        pull = self.post_json(f"{prefix}/pulls", {
            "title": title, "head": branch, "base": base_branch, "body": body, "draft": True,
        })
        pull_id = pull.get("id")
        number = pull.get("number")
        url = pull.get("html_url")
        head_sha = (pull.get("head") or {}).get("sha")
        if (
            not isinstance(pull_id, int) or not isinstance(number, int) or not isinstance(url, str)
            or not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", head_sha)
        ):
            raise GitHubRepositoryError("GitHub returned invalid pull request metadata")
        _validate_pull_url(url)
        return {"id": pull_id, "number": number, "url": url, "branch": branch, "commit_sha": head_sha}

    def gather_context(self, full_name: str, commit_sha: str, failure_summary: str, workflow_run_id: int | None) -> dict:
        parts = full_name.split("/")
        if len(parts) != 2 or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", part) for part in parts):
            raise GitHubRepositoryError("Repository name is invalid")
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit_sha):
            raise GitHubRepositoryError("Commit identifier is invalid")
        owner, repo = parts
        prefix = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        commit = self.get_json(f"{prefix}/commits/{commit_sha}")
        changed_files = commit.get("files") or []
        file_summaries = []
        source_files: dict[str, str] = {}
        used_bytes = 0
        for item in changed_files[:50]:
            filename = item.get("filename")
            if not isinstance(filename, str) or not self._safe_source_path(filename):
                continue
            file_summaries.append({
                "path": filename,
                "status": item.get("status"),
                "additions": int(item.get("additions") or 0),
                "deletions": int(item.get("deletions") or 0),
                "patch_excerpt": redact_text(str(item.get("patch") or ""))[:4000],
            })
            if len(source_files) >= MAX_FILES or self._extension(filename) not in TEXT_EXTENSIONS:
                continue
            content = self._read_text_file(prefix, filename, commit_sha)
            if content is None:
                continue
            encoded_size = len(content.encode("utf-8"))
            if encoded_size > MAX_FILE_BYTES or used_bytes + encoded_size > MAX_TOTAL_SOURCE_BYTES:
                continue
            source_files[filename] = redact_text(content)
            used_bytes += encoded_size

        tree_sha = ((commit.get("commit") or {}).get("tree") or {}).get("sha")
        repository_files: list[str] = []
        if isinstance(tree_sha, str) and re.fullmatch(r"[0-9a-f]{40,64}", tree_sha):
            tree = self.get_json(f"{prefix}/git/trees/{tree_sha}", params={"recursive": "1"})
            nodes = tree.get("tree") or []
            repository_files = [
                item["path"] for item in nodes[:1000]
                if isinstance(item, dict) and item.get("type") == "blob" and isinstance(item.get("path"), str)
            ][:200]
            test_candidates = [
                path for path in repository_files
                if ("/tests/" in f"/{path}/" or path.rsplit("/", 1)[-1].lower().startswith(("test_", "tests.")))
                and self._safe_source_path(path) and self._extension(path) in TEXT_EXTENSIONS
            ]
            dependency_candidates = [
                path for path in repository_files
                if path.rsplit("/", 1)[-1] in DEPENDENCY_FILES and self._safe_source_path(path)
            ]
            for path in test_candidates:
                if len(source_files) >= MAX_FILES:
                    break
                if path in source_files:
                    continue
                content = self._read_text_file(prefix, path, commit_sha)
                if content is None:
                    continue
                encoded_size = len(content.encode("utf-8"))
                if encoded_size > MAX_FILE_BYTES or used_bytes + encoded_size > MAX_TOTAL_SOURCE_BYTES:
                    continue
                source_files[path] = redact_text(content)
                used_bytes += encoded_size

            while True:
                referenced_sources = [
                    path for path in self._referenced_local_python_sources(source_files, repository_files)
                    if path not in source_files
                ]
                if not referenced_sources:
                    break
                added_source = False
                for path in referenced_sources:
                    if len(source_files) >= MAX_FILES:
                        break
                    content = self._read_text_file(prefix, path, commit_sha)
                    if content is None:
                        continue
                    encoded_size = len(content.encode("utf-8"))
                    if encoded_size > MAX_FILE_BYTES or used_bytes + encoded_size > MAX_TOTAL_SOURCE_BYTES:
                        continue
                    source_files[path] = redact_text(content)
                    used_bytes += encoded_size
                    added_source = True
                if not added_source or len(source_files) >= MAX_FILES:
                    break

            for path in dependency_candidates:
                if len(source_files) >= MAX_FILES:
                    break
                if path in source_files:
                    continue
                content = self._read_text_file(prefix, path, commit_sha)
                if content is None:
                    continue
                encoded_size = len(content.encode("utf-8"))
                if encoded_size > MAX_FILE_BYTES or used_bytes + encoded_size > MAX_TOTAL_SOURCE_BYTES:
                    continue
                source_files[path] = redact_text(content)
                used_bytes += encoded_size

        if not file_summaries:
            raise GitHubRepositoryError("No safe changed source files were available for context gathering")
        languages = self.get_json(f"{prefix}/languages")
        workflow_log_excerpt = self._failed_job_logs(prefix, workflow_run_id) if workflow_run_id else ""
        python_analysis = {
            path: self._analyze_python(text)
            for path, text in source_files.items()
            if path.endswith(".py")
        }
        return {
            "repository": full_name,
            "commit_sha": commit_sha,
            "commit_message": redact_text(str((commit.get("commit") or {}).get("message") or ""))[:2000],
            "failure_summary": redact_text(failure_summary)[:3000],
            "failure_log_excerpt": workflow_log_excerpt,
            "failing_test_selector": _extract_failing_test_selector(f"{failure_summary}\n{workflow_log_excerpt}"),
            "changed_files": file_summaries,
            "repository_files": repository_files,
            "languages": languages,
            "source_files": source_files,
            "python_analysis": python_analysis,
        }

    def download_source_archive(self, full_name: str, commit_sha: str, destination: Path) -> None:
        parts = full_name.split("/")
        if len(parts) != 2 or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", part) for part in parts):
            raise GitHubRepositoryError("Repository name is invalid")
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit_sha):
            raise GitHubRepositoryError("Commit identifier is invalid")
        owner, repo = parts
        response = self._request("GET", f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/zipball/{commit_sha}")
        location = response.headers.get("location")
        parsed = urlparse(location or "")
        if (
            response.status_code != 302 or parsed.scheme != "https" or parsed.hostname != "codeload.github.com"
            or parsed.port not in {None, 443} or parsed.username or parsed.password or parsed.fragment
        ):
            raise GitHubRepositoryError("GitHub did not return a trusted source archive")
        try:
            with httpx.Client(timeout=httpx.Timeout(30.0, connect=5.0), follow_redirects=False) as client:
                with client.stream("GET", location) as archive_response:
                    if archive_response.status_code != 200:
                        raise GitHubRepositoryError("GitHub source archive could not be downloaded")
                    archive = bytearray()
                    for chunk in archive_response.iter_bytes():
                        if len(archive) + len(chunk) > MAX_ARCHIVE_BYTES:
                            raise GitHubRepositoryError("Repository source archive exceeds the size limit")
                        archive.extend(chunk)
        except httpx.RequestError as exc:
            raise GitHubRepositoryError("GitHub source archive could not be reached") from exc
        self._extract_archive(bytes(archive), destination)

    @classmethod
    def _extract_archive(cls, archive: bytes, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
                infos = zipped.infolist()
                if len(infos) > MAX_ARCHIVE_FILES * 2:
                    raise GitHubRepositoryError("Repository source archive contains too many entries")
                root_prefix: str | None = None
                total_bytes = 0
                written_files = 0
                for info in infos:
                    raw_name = info.filename
                    if "\\" in raw_name:
                        raise GitHubRepositoryError("Repository source archive has an unsafe path")
                    archive_path = PurePosixPath(raw_name)
                    if archive_path.is_absolute() or any(part in {"", ".", ".."} for part in archive_path.parts) or any(":" in part for part in archive_path.parts):
                        raise GitHubRepositoryError("Repository source archive has an unsafe path")
                    if root_prefix is None:
                        root_prefix = archive_path.parts[0]
                    if archive_path.parts[0] != root_prefix:
                        raise GitHubRepositoryError("Repository source archive has an invalid root layout")
                    relative_parts = archive_path.parts[1:]
                    if not relative_parts or info.is_dir():
                        continue
                    if not cls._safe_source_path("/".join(relative_parts)):
                        continue
                    mode = info.external_attr >> 16
                    file_type = stat.S_IFMT(mode)
                    if stat.S_ISLNK(mode) or (file_type and file_type != stat.S_IFREG):
                        continue
                    if info.file_size < 0 or info.file_size > 50_000_000:
                        raise GitHubRepositoryError("Repository source archive contains an oversized file")
                    total_bytes += info.file_size
                    written_files += 1
                    if total_bytes > MAX_EXTRACTED_BYTES or written_files > MAX_ARCHIVE_FILES:
                        raise GitHubRepositoryError("Repository source archive exceeds extraction limits")
                    output_path = destination.joinpath(*relative_parts)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    with zipped.open(info, "r") as source, output_path.open("wb") as target:
                        remaining = info.file_size
                        while remaining:
                            chunk = source.read(min(64_000, remaining))
                            if not chunk:
                                raise GitHubRepositoryError("Repository source archive is truncated")
                            target.write(chunk)
                            remaining -= len(chunk)
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            raise GitHubRepositoryError("GitHub returned an invalid repository source archive") from exc

    @staticmethod
    def _analyze_python(source: str) -> dict:
        try:
            tree = ast.parse(source)
        except (SyntaxError, RecursionError):
            return {"parseable": False, "imports": [], "definitions": []}
        imports: list[str] = []
        definitions: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                definitions.append(node.name)
        return {
            "parseable": True,
            "imports": sorted(set(imports))[:100],
            "definitions": definitions[:100],
        }

    @staticmethod
    def _referenced_local_python_sources(source_files: dict[str, str], repository_files: list[str]) -> list[str]:
        module_paths: dict[str, str] = {}
        for path in repository_files:
            if not path.endswith(".py") or not GitHubRepositoryClient._safe_source_path(path):
                continue
            module = path[:-3].replace("/", ".")
            if module.endswith(".__init__"):
                module = module[:-9]
            module_paths[module] = path

        referenced_modules: set[str] = set()
        for path, source in source_files.items():
            if not path.endswith(".py"):
                continue
            try:
                tree = ast.parse(source)
            except (SyntaxError, RecursionError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    referenced_modules.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    referenced_modules.add(node.module)
                    referenced_modules.update(f"{node.module}.{alias.name}" for alias in node.names)

        matches: set[str] = set()
        for imported in referenced_modules:
            for module, path in module_paths.items():
                if module == imported or module.endswith("." + imported) or imported.endswith("." + module):
                    matches.add(path)
        return sorted(matches)

    @staticmethod
    def _extension(path: str) -> str:
        name = path.rsplit("/", 1)[-1]
        if name.lower().startswith("requirements"):
            return ".txt"
        return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    @classmethod
    def _safe_source_path(cls, path: str) -> bool:
        if path.startswith("/") or "\\" in path or any(part in {".", ".."} for part in path.split("/")):
            return False
        parts = path.split("/")
        lowered = [part.lower() for part in parts]
        return not any(
            part in PROTECTED_NAMES or part.startswith(".env.") or part.endswith((".pem", ".key", ".p12", ".pfx"))
            for part in lowered
        )

    def _read_text_file(self, prefix: str, path: str, sha: str) -> str | None:
        safe_path = "/".join(quote(part, safe="") for part in path.split("/"))
        try:
            data = self.get_json(f"{prefix}/contents/{safe_path}", params={"ref": sha})
        except GitHubRepositoryError:
            return None
        if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
            return None
        try:
            content = base64.b64decode(data["content"], validate=True)
            text = content.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        return text[:MAX_FILE_BYTES]

    def _failed_job_logs(self, prefix: str, workflow_run_id: int) -> str:
        if workflow_run_id <= 0:
            return ""
        jobs_response = self.get_json(f"{prefix}/actions/runs/{workflow_run_id}/jobs", params={"per_page": 100, "filter": "latest"})
        jobs = jobs_response.get("jobs") or []
        failures = [item for item in jobs if item.get("conclusion") in {"failure", "timed_out"}][:5]
        excerpts = []
        for job in failures:
            job_id = job.get("id")
            if not isinstance(job_id, int):
                continue
            redirect = self._request("GET", f"{prefix}/actions/jobs/{job_id}/logs")
            location = redirect.headers.get("location")
            if redirect.status_code != 302 or not location:
                continue
            excerpt = self._download_log(location)
            if excerpt:
                excerpts.append(f"### {job.get('name') or 'failed job'}\n{excerpt}")
        return redact_text("\n\n".join(excerpts))[-20_000:]

    def _download_log(self, location: str) -> str:
        parsed = urlparse(location)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or parsed.port not in {None, 443} or parsed.username or parsed.password or not any(host.endswith(suffix) for suffix in ALLOWED_LOG_HOST_SUFFIXES):
            return ""
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
            if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
                return ""
        except (OSError, ValueError):
            return ""
        try:
            with httpx.Client(timeout=httpx.Timeout(8.0, connect=3.0), follow_redirects=False) as client:
                with client.stream("GET", location, headers={"Accept": "text/plain"}) as response:
                    if response.status_code != 200:
                        return ""
                    chunks = bytearray()
                    for chunk in response.iter_bytes():
                        if len(chunks) + len(chunk) > MAX_LOG_BYTES:
                            break
                        chunks.extend(chunk)
                    return chunks.decode("utf-8", errors="replace")[-20_000:]
        except httpx.RequestError:
            return ""


def _plain_pr_text(value: str, limit: int) -> str:
    safe = redact_text(value)
    safe = re.sub(r"[\x00-\x1f\x7f]+", " ", safe)
    return safe.replace("`", "'").replace("[", "(").replace("]", ")").replace("<", "&lt;").replace(">", "&gt;").replace("@", " at ").strip()[:limit]


def _validate_pull_url(value: str) -> None:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.port not in {None, 443}
        or parsed.username or parsed.password
        or not re.fullmatch(r"/[^/]+/[^/]+/pull/[1-9][0-9]*", parsed.path)
    ):
        raise GitHubRepositoryError("GitHub returned an untrusted pull request URL")


def _extract_failing_test_selector(log_excerpt: str) -> str | None:
    """Extract one safe pytest node ID from standard failure summary lines."""
    clean_excerpt = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", log_excerpt)
    for line in clean_excerpt.splitlines():
        match = re.match(r"\s*FAILED\s+([^\s]+)", line)
        if not match:
            continue
        selector = match.group(1)
        path, separator, node_id = selector.partition("::")
        path_parts = path.split("/")
        node_parts = node_id.split("::") if separator else []
        if (
            len(selector) > 300 or not separator or not node_id or not path.endswith(".py")
            or path.startswith("/") or "\\" in path or any(part in {"", ".", ".."} for part in path_parts)
            or path_parts[0].startswith("-")
            or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in path_parts)
            or not all(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:-]*(?:\[[A-Za-z0-9_.,=:+-]{1,100}\])?", part) for part in node_parts)
        ):
            continue
        return selector
    return None
