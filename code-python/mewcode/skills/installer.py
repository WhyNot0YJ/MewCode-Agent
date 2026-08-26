# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
"""Skill 远程安装：支持 git 仓库、直链 .md 文件与本地路径三种来源。

安装流程：
1. 识别来源类型（git URL / http(s) .md 直链 / 本地路径）；
2. 拉取内容到临时目录（git clone --depth 1）或直接下载；
3. 定位 SKILL.md（仓库根目录、单个 skill 子目录、或批量安装多个子目录）；
4. 复制到目标 skills 目录（默认用户级 ~/.mewcode/skills，可指定项目级）；
5. 调用 loader.load_all() 热刷新注册表。
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from mewcode.skills.loader import SkillLoader
from mewcode.skills.parser import SkillParseError, parse_skill_file

log = logging.getLogger(__name__)

GIT_TIMEOUT_SECONDS = 120
HTTP_TIMEOUT_SECONDS = 30
MAX_INSTALL_FILES = 200


class SkillInstallError(Exception):
    pass


@dataclass
class InstallResult:
    name: str
    path: Path
    scope: str  # "user" | "project"


def _is_git_url(source: str) -> bool:
    s = source.strip().lower()
    if s.startswith("git@"):
        return True
    if s.startswith(("https://", "http://")) and s.endswith(".git"):
        return True
    # github/gitlab 短格式：owner/repo 也可以直接 clone
    return False


def _is_http_url(source: str) -> bool:
    return source.strip().lower().startswith(("https://", "http://"))


def _target_dir(loader: SkillLoader, scope: str) -> Path:
    """安装目标目录：user 级 ~/.mewcode/skills，project 级 .mewcode/skills。"""
    if scope == "project":
        return Path(loader._work_dir) / ".mewcode" / "skills"
    return Path.home() / ".mewcode" / "skills"


def _validate_git_source(source: str) -> str:
    s = source.strip()
    if s.startswith("git@") or s.startswith(("https://", "http://")):
        return s
    raise SkillInstallError(
        f"不支持的 git 来源格式: {source}\n"
        "支持 git@host:owner/repo.git 或 https://host/owner/repo.git"
    )


async def _git_clone(source: str, dest: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "1", source, str(dest),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=GIT_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise SkillInstallError(f"git clone 超时（>{GIT_TIMEOUT_SECONDS}s）: {source}")
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip() if stderr else ""
        raise SkillInstallError(f"git clone 失败: {detail or source}")


async def _download_markdown(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text
    except httpx.HTTPError as e:
        raise SkillInstallError(f"下载失败: {e}") from e


def _find_skill_dirs(root: Path) -> list[Path]:
    """在克隆的仓库中定位 skill：根目录 SKILL.md、单层子目录 SKILL.md。"""
    candidates: list[Path] = []
    root_skill = root / "SKILL.md"
    if root_skill.is_file():
        candidates.append(root)
    for child in sorted(root.iterdir()):
        if child.is_dir() and child.name not in (".git",) and (child / "SKILL.md").is_file():
            candidates.append(child)
    return candidates


def _install_skill_dir(skill_dir: Path, target_base: Path) -> tuple[str, Path]:
    """把单个 skill 目录复制到目标目录，返回 (name, dest)。"""
    try:
        skill = parse_skill_file(skill_dir / "SKILL.md")
    except SkillParseError as e:
        raise SkillInstallError(f"无效的 SKILL.md（{skill_dir}）: {e}") from e

    dest = target_base / skill.name
    if dest.exists():
        raise SkillInstallError(
            f"已存在同名 Skill '{skill.name}'（{dest}）。如需覆盖请先删除旧目录。"
        )

    file_count = sum(1 for _ in skill_dir.rglob("*") if _.is_file())
    if file_count > MAX_INSTALL_FILES:
        raise SkillInstallError(
            f"Skill 目录文件数过多（{file_count} > {MAX_INSTALL_FILES}），疑似误选目录"
        )

    target_base.mkdir(parents=True, exist_ok=True)
    shutil.copytree(skill_dir, dest, ignore=shutil.ignore_patterns(".git"))
    return skill.name, dest


def _install_markdown_file(md_path: Path, target_base: Path) -> tuple[str, Path]:
    try:
        skill = parse_skill_file(md_path)
    except SkillParseError as e:
        raise SkillInstallError(f"无效的 Skill 文件（{md_path.name}）: {e}") from e
    dest = target_base / f"{skill.name}.md"
    if dest.exists():
        raise SkillInstallError(
            f"已存在同名 Skill '{skill.name}'（{dest}）。如需覆盖请先删除旧文件。"
        )
    target_base.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(md_path, dest)
    return skill.name, dest


async def install_skill(
    source: str,
    loader: SkillLoader,
    scope: str = "user",
) -> list[InstallResult]:
    """安装一个 Skill（git 仓库可批量安装其中包含的多个 skill）。

    返回安装结果列表；完成后 loader 的注册表已通过 load_all() 刷新。
    """
    source = source.strip()
    if not source:
        raise SkillInstallError("缺少安装来源")

    if scope not in ("user", "project"):
        raise SkillInstallError(f"无效的 scope: {scope}（可选 user / project）")

    target_base = _target_dir(loader, scope)
    results: list[InstallResult] = []

    # --- 来源 1：本地路径 ---
    local = Path(source).expanduser()
    if local.exists() and not _is_http_url(source) and not _is_git_url(source):
        if local.is_dir():
            if not (local / "SKILL.md").is_file():
                raise SkillInstallError(f"目录中没有 SKILL.md: {local}")
            name, dest = _install_skill_dir(local, target_base)
            results.append(InstallResult(name=name, path=dest, scope=scope))
        else:
            name, dest = _install_markdown_file(local, target_base)
            results.append(InstallResult(name=name, path=dest, scope=scope))

    # --- 来源 2：http(s) 直链 .md ---
    elif _is_http_url(source):
        content = await _download_markdown(source)
        if "---" not in content[:200]:
            raise SkillInstallError("下载的内容不是有效的 Skill（缺少 YAML frontmatter）")
        with tempfile.TemporaryDirectory(prefix="mewcode-skill-") as tmp:
            tmp_md = Path(tmp) / "downloaded.md"
            tmp_md.write_text(content, encoding="utf-8")
            name, dest = _install_markdown_file(tmp_md, target_base)
        results.append(InstallResult(name=name, path=dest, scope=scope))

    # --- 来源 3：git 仓库 ---
    else:
        url = _validate_git_source(source)
        with tempfile.TemporaryDirectory(prefix="mewcode-skill-clone-") as tmp:
            clone_dir = Path(tmp) / "repo"
            await _git_clone(url, clone_dir)
            skill_dirs = _find_skill_dirs(clone_dir)
            if not skill_dirs:
                raise SkillInstallError(
                    "仓库中没有找到任何 SKILL.md（支持根目录或一级子目录）"
                )
            for skill_dir in skill_dirs:
                name, dest = _install_skill_dir(skill_dir, target_base)
                results.append(InstallResult(name=name, path=dest, scope=scope))

    # 热刷新注册表，安装后立即可用
    loader.load_all()
    for r in results:
        log.info("Installed skill '%s' to %s (scope=%s)", r.name, r.path, r.scope)
    return results


def make_install_summary(results: list[InstallResult]) -> str:
    lines = [f"已安装 {len(results)} 个 Skill："]
    for r in results:
        lines.append(f"  {r.name}  ->  {r.path}  [{r.scope}]")
    return "\n".join(lines)
