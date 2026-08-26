# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com

"""恢复策略与新增能力的测试。

覆盖：
- agent.py 的限流退避 / 网络重试 / 上下文超限被动恢复（_llm_stream_with_retry）
- skills/installer.py 的远程安装（本地路径 / URL 判定 / 目录安装 / 冲突检测）
- tools/task_stop.py、task_output.py 的后台任务管理
- prompts.py 的 git 状态环境注入
- memory/session.py 的 session_mode 持久化与时间间隔提醒
- teams/coordinator.py 的 match_session_mode 状态迁移
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import mewcode.agent as agent_module
from mewcode.agent import Agent, CompactNotification, RetryEvent
from mewcode.client import (
    LLMClient,
    LLMError,
    NetworkError,
    RateLimitError,
)
from mewcode.conversation import ConversationManager, Message
from mewcode.memory.session import SessionMeta, build_time_gap_message
from mewcode.prompts import _git_state, build_environment_context, environment_section
from mewcode.teams.coordinator import match_session_mode
from mewcode.tools import create_default_registry
from mewcode.tools.base import StreamEnd, TextDelta


# ---------------------------------------------------------------------------
# Mock：按脚本执行的 LLM 客户端
# ---------------------------------------------------------------------------

class ScriptedClient(LLMClient):
    """每次 stream() 调用消费脚本中的一项：异常则抛出，列表则逐个产出事件。"""

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.calls = 0

    async def stream(self, conversation, system: str = "", tools=None):
        self.calls += 1
        step = self._script[self.calls - 1] if self.calls <= len(self._script) else None
        if isinstance(step, BaseException):
            raise step
        if step is None:
            yield TextDelta("fallback")
            yield StreamEnd("end_turn", 1, 1)
        else:
            for e in step:
                yield e


def _make_agent(client: LLMClient, tmp_path: Path) -> Agent:
    return Agent(
        client,
        create_default_registry(),
        "anthropic",
        work_dir=str(tmp_path),
    )


def _build_long_conversation(n: int = 120, chars: int = 600) -> ConversationManager:
    """构造足以触发 compact 的长对话（~20K tokens）。"""
    conv = ConversationManager()
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        conv.history.append(Message(role=role, content=f"msg-{i}: " + "x" * chars))
    return conv


async def _drain(agen) -> list:
    return [e async for e in agen]


# ---------------------------------------------------------------------------
# is_context_overflow_error
# ---------------------------------------------------------------------------

def test_is_context_overflow_error_matches_provider_phrasings():
    from mewcode.agent import is_context_overflow_error

    assert is_context_overflow_error("prompt is too long: 250000 tokens > 200000 maximum")
    assert is_context_overflow_error("Error: context length exceeded")
    assert is_context_overflow_error("This model's maximum context length is 8192 tokens")
    assert is_context_overflow_error("Input tokens exceed the limit")
    assert is_context_overflow_error("requested tokens exceed context window")
    # 非超限错误不应误判
    assert not is_context_overflow_error("authentication failed")
    assert not is_context_overflow_error("internal server error")
    assert not is_context_overflow_error("overloaded")


# ---------------------------------------------------------------------------
# 限流退避
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limit_retry_recovers(tmp_path, monkeypatch):
    """429 后按退避重试，第二次成功，流式事件完整送达。"""
    monkeypatch.setattr(agent_module, "RATE_LIMIT_BASE_DELAY", 0)
    monkeypatch.setattr(agent_module, "RATE_LIMIT_MAX_DELAY", 0)

    client = ScriptedClient([
        RateLimitError("rate limited"),
        [TextDelta("recovered"), StreamEnd("end_turn", 10, 5)],
    ])
    agent = _make_agent(client, tmp_path)
    conv = ConversationManager()
    conv.add_user_message("hi")

    events = await _drain(
        agent._llm_stream_with_retry(conv, conv, system="", tools=[])
    )

    retries = [e for e in events if isinstance(e, RetryEvent)]
    assert len(retries) == 1
    assert "rate limited" in retries[0].reason
    assert agent._last_collector.response.text == "recovered"
    assert client.calls == 2


@pytest.mark.asyncio
async def test_rate_limit_uses_retry_after(tmp_path, monkeypatch):
    """服务端给了 retry_after 时优先尊重它，而不是指数退避。"""
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    client = ScriptedClient([
        RateLimitError("rate limited", retry_after=7.5),
        [TextDelta("ok"), StreamEnd("end_turn", 1, 1)],
    ])
    agent = _make_agent(client, tmp_path)
    conv = ConversationManager()

    await _drain(agent._llm_stream_with_retry(conv, conv, system="", tools=[]))

    assert recorded == [7.5]


@pytest.mark.asyncio
async def test_rate_limit_exhaustion_raises(tmp_path, monkeypatch):
    """超过最大重试次数后错误向上抛出。"""
    monkeypatch.setattr(agent_module, "RATE_LIMIT_BASE_DELAY", 0)
    monkeypatch.setattr(agent_module, "RATE_LIMIT_MAX_DELAY", 0)

    client = ScriptedClient([RateLimitError("rate limited")] * 10)
    agent = _make_agent(client, tmp_path)
    conv = ConversationManager()

    with pytest.raises(RateLimitError):
        await _drain(agent._llm_stream_with_retry(conv, conv, system="", tools=[]))

    # 1 次原始调用 + 5 次重试 = 6
    assert client.calls == 1 + agent_module.RATE_LIMIT_MAX_RETRIES


# ---------------------------------------------------------------------------
# 网络错误重试
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_network_error_retry_recovers(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "NETWORK_BASE_DELAY", 0)

    client = ScriptedClient([
        NetworkError("connection reset"),
        NetworkError("connection timeout"),
        [TextDelta("back online"), StreamEnd("end_turn", 1, 1)],
    ])
    agent = _make_agent(client, tmp_path)
    conv = ConversationManager()

    events = await _drain(agent._llm_stream_with_retry(conv, conv, system="", tools=[]))

    retries = [e for e in events if isinstance(e, RetryEvent)]
    assert len(retries) == 2
    assert all("network error" in r.reason for r in retries)
    assert agent._last_collector.response.text == "back online"


# ---------------------------------------------------------------------------
# 上下文超限被动恢复
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_context_overflow_forces_compact_and_retries(tmp_path):
    """主请求报 prompt too long → 强制 compact → 重建 api_conv → 重试成功。"""
    client = ScriptedClient([
        # 1. 主请求：上下文超限
        LLMError("prompt is too long: 250000 tokens > 200000 maximum"),
        # 2. auto_compact 的摘要请求
        [TextDelta("<summary>early work summary</summary>"),
         StreamEnd("end_turn", 10, 10)],
        # 3. 压缩后的重试主请求
        [TextDelta("final answer"), StreamEnd("end_turn", 10, 10)],
    ])
    agent = _make_agent(client, tmp_path)
    conv = _build_long_conversation()

    events = await _drain(
        agent._llm_stream_with_retry(conv, conv, system="", tools=[])
    )

    # 恢复过程产生 RetryEvent + CompactNotification
    retry = [e for e in events if isinstance(e, RetryEvent)]
    assert len(retry) == 1
    assert "context window" in retry[0].reason
    notes = [e for e in events if isinstance(e, CompactNotification)]
    assert len(notes) == 1
    assert "已强制压缩" in notes[0].message

    # 原始对话被压缩重建：首条消息携带摘要，长度远小于压缩前
    assert "early work summary" in conv.history[0].content
    assert len(conv.history) < 130

    # 重试后的响应通过 collector 暴露
    assert agent._last_collector.response.text == "final answer"
    assert client.calls == 3


@pytest.mark.asyncio
async def test_context_overflow_repeated_beyond_limit_raises(tmp_path):
    """压缩后仍然超限、超过恢复次数上限时向上抛出。"""
    client = ScriptedClient([
        LLMError("prompt is too long"),                       # 主请求 1：失败 → compact
        [TextDelta("<summary>s</summary>"),                   # 摘要请求
         StreamEnd("end_turn", 1, 1)],
        LLMError("prompt is too long"),                       # 主请求 2：仍失败
    ])
    agent = _make_agent(client, tmp_path)
    conv = _build_long_conversation()

    with pytest.raises(LLMError):
        await _drain(agent._llm_stream_with_retry(conv, conv, system="", tools=[]))

    # 第二次 compact 因前缀太小而放弃（历史已被压短），错误直接上抛
    assert client.calls == 3


@pytest.mark.asyncio
async def test_non_overflow_llm_error_raises_immediately(tmp_path):
    """非超限类 LLM 错误（如鉴权失败）不做恢复，直接抛出。"""
    client = ScriptedClient([LLMError("authentication failed")])
    agent = _make_agent(client, tmp_path)
    conv = ConversationManager()

    with pytest.raises(LLMError, match="authentication"):
        await _drain(agent._llm_stream_with_retry(conv, conv, system="", tools=[]))

    assert client.calls == 1


# ---------------------------------------------------------------------------
# Skill 安装器
# ---------------------------------------------------------------------------

SKILL_MD = """---
name: my-skill
description: A test skill
---
Do the thing.
"""


@pytest.fixture
def skill_loader(tmp_path: Path):
    from mewcode.skills.loader import SkillLoader

    return SkillLoader(str(tmp_path))


def test_git_url_detection():
    from mewcode.skills.installer import _is_git_url, _is_http_url

    assert _is_git_url("git@github.com:owner/repo.git")
    assert _is_git_url("https://github.com/owner/repo.git")
    assert not _is_git_url("https://example.com/skill.md")
    assert not _is_git_url("/local/path")
    assert _is_http_url("https://example.com/skill.md")
    assert not _is_http_url("git@github.com:owner/repo.git")


@pytest.mark.asyncio
async def test_install_skill_from_local_markdown(tmp_path, skill_loader):
    from mewcode.skills.installer import install_skill

    src = tmp_path / "incoming-skill.md"
    src.write_text(SKILL_MD, encoding="utf-8")

    results = await install_skill(str(src), skill_loader, scope="project")

    assert len(results) == 1
    assert results[0].name == "my-skill"
    assert results[0].scope == "project"
    dest = tmp_path / ".mewcode" / "skills" / "my-skill.md"
    assert dest.is_file()

    # 安装后注册表热刷新，立即可用
    loaded = skill_loader.load_all()
    assert "my-skill" in loaded


@pytest.mark.asyncio
async def test_install_skill_from_local_dir(tmp_path, skill_loader):
    from mewcode.skills.installer import install_skill

    src_dir = tmp_path / "incoming" / "packaged-skill"
    src_dir.mkdir(parents=True)
    (src_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (src_dir / "helper.py").write_text("print('helper')\n", encoding="utf-8")

    results = await install_skill(str(src_dir), skill_loader, scope="project")

    assert results[0].name == "my-skill"
    dest = tmp_path / ".mewcode" / "skills" / "my-skill"
    assert (dest / "SKILL.md").is_file()
    assert (dest / "helper.py").is_file()


@pytest.mark.asyncio
async def test_install_skill_duplicate_rejected(tmp_path, skill_loader):
    from mewcode.skills.installer import SkillInstallError, install_skill

    src = tmp_path / "dup.md"
    src.write_text(SKILL_MD, encoding="utf-8")

    await install_skill(str(src), skill_loader, scope="project")
    with pytest.raises(SkillInstallError, match="已存在同名"):
        await install_skill(str(src), skill_loader, scope="project")


@pytest.mark.asyncio
async def test_install_skill_invalid_inputs(tmp_path, skill_loader):
    from mewcode.skills.installer import SkillInstallError, install_skill

    src = tmp_path / "ok.md"
    src.write_text(SKILL_MD, encoding="utf-8")

    # 非法 scope
    with pytest.raises(SkillInstallError, match="scope"):
        await install_skill(str(src), skill_loader, scope="bogus")

    # 无法识别的来源（既不是存在的路径，也不是合法 git URL）
    with pytest.raises(SkillInstallError, match="git 来源"):
        await install_skill("definitely-not-a-source-$$$", skill_loader)

    # 目录里没有 SKILL.md
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(SkillInstallError, match="SKILL.md"):
        await install_skill(str(empty_dir), skill_loader, scope="project")

    # 无 frontmatter 的 markdown
    bad = tmp_path / "bad.md"
    bad.write_text("no frontmatter here", encoding="utf-8")
    with pytest.raises(SkillInstallError, match="无效"):
        await install_skill(str(bad), skill_loader, scope="project")


@pytest.mark.asyncio
async def test_install_skill_to_user_scope(tmp_path, monkeypatch):
    from mewcode.skills.installer import install_skill
    from mewcode.skills.loader import SkillLoader

    # 把 home 指到临时目录，避免污染真实 ~/.mewcode
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    work = tmp_path / "work"
    work.mkdir()
    loader = SkillLoader(str(work))

    src = tmp_path / "user-skill.md"
    src.write_text(SKILL_MD, encoding="utf-8")

    results = await install_skill(str(src), loader, scope="user")

    assert results[0].scope == "user"
    assert (fake_home / ".mewcode" / "skills" / "my-skill.md").is_file()


# ---------------------------------------------------------------------------
# TaskStop / TaskOutput
# ---------------------------------------------------------------------------

class StubAgent:
    """task_manager 所需的最小 Agent 接口。"""

    def __init__(self, delay: float = 0.2) -> None:
        self.agent_id = "agent-stub"
        self.team_name = ""  # 非队友：不进入 mailbox 等待循环
        self._team_manager = None
        self.total_input_tokens = 120
        self.total_output_tokens = 80
        self._delay = delay

    async def run_to_completion(self, task, conversation=None, event_callback=None):
        await asyncio.sleep(self._delay)
        return f"finished: {task}"


@pytest.mark.asyncio
async def test_task_stop_cancels_running_task():
    from mewcode.agents.task_manager import TaskManager
    from mewcode.tools.task_stop import TaskStopParams, TaskStopTool

    tm = TaskManager()
    task_id = tm.launch(StubAgent(), "long work", name="worker-1")

    tool = TaskStopTool(tm)
    result = await tool.execute(TaskStopParams(task_id=task_id))

    assert not result.is_error
    assert "stopped" in result.output
    assert "SendMessage" in result.output  # 提示可继续该 worker

    await asyncio.sleep(0.05)  # 让取消传播完成
    assert tm.get(task_id).status == "cancelled"


@pytest.mark.asyncio
async def test_task_stop_by_name_and_unknown():
    from mewcode.agents.task_manager import TaskManager
    from mewcode.tools.task_stop import TaskStopParams, TaskStopTool

    tm = TaskManager()
    tm.launch(StubAgent(), "work", name="alice")

    tool = TaskStopTool(tm)
    # 按名称解析
    result = await tool.execute(TaskStopParams(task_id="alice"))
    assert not result.is_error

    # 不存在的任务
    missing = await tool.execute(TaskStopParams(task_id="ghost"))
    assert missing.is_error
    assert "no running task" in missing.output

    await asyncio.sleep(0.05)
    # 已停止的任务再次 stop 是 no-op
    again = await tool.execute(TaskStopParams(task_id="alice"))
    assert not again.is_error
    assert "not running" in again.output


@pytest.mark.asyncio
async def test_task_output_reports_status_and_result():
    from mewcode.agents.task_manager import TaskManager
    from mewcode.tools.task_output import TaskOutputParams, TaskOutputTool

    tm = TaskManager()
    task_id = tm.launch(StubAgent(delay=0.05), "quick job", name="bob")

    tool = TaskOutputTool(tm)

    running = await tool.execute(TaskOutputParams(task_id=task_id))
    assert not running.is_error
    assert "running" in running.output

    await asyncio.sleep(0.2)  # 等任务完成
    done = await tool.execute(TaskOutputParams(task_id="bob"))
    assert "completed" in done.output
    assert "finished: quick job" in done.output
    assert "120" in done.output  # token 用量

    unknown = await tool.execute(TaskOutputParams(task_id="nobody"))
    assert unknown.is_error
    assert "no task found" in unknown.output


# ---------------------------------------------------------------------------
# 环境上下文中的 git 状态
# ---------------------------------------------------------------------------

def test_git_state_non_git_dir(tmp_path):
    branch, dirty = _git_state(str(tmp_path))
    assert branch is None
    assert dirty == 0


def test_git_state_reports_branch_and_dirty(tmp_path):
    def run_git(*args):
        subprocess.run(
            ["git", *args], cwd=str(repo), check=True,
            capture_output=True, timeout=10,
        )

    repo = tmp_path / "repo"
    repo.mkdir()
    run_git("init", "-q")
    run_git("config", "user.email", "test@example.com")
    run_git("config", "user.name", "test")
    (repo / "a.txt").write_text("x", encoding="utf-8")
    run_git("add", ".")
    run_git("commit", "-qm", "init")

    branch, dirty = _git_state(str(repo))
    assert branch  # main / master 均可
    assert dirty == 0

    (repo / "b.txt").write_text("y", encoding="utf-8")
    _, dirty2 = _git_state(str(repo))
    assert dirty2 == 1


def test_environment_section_includes_git_state(tmp_path):
    section = environment_section(str(tmp_path))
    assert "Working directory" in section.content
    assert "Git branch" not in section.content  # tmp 目录不是 git 仓库


def test_build_environment_context_includes_git_state(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q"], cwd=str(repo), check=True,
        capture_output=True, timeout=10,
    )
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"], cwd=str(repo), check=True,
        capture_output=True, timeout=10,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=str(repo), check=True,
        capture_output=True, timeout=10,
    )
    (repo / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True,
                   capture_output=True, timeout=10)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True,
                   capture_output=True, timeout=10)
    ctx = build_environment_context(str(repo))
    assert "Git branch" in ctx


# ---------------------------------------------------------------------------
# 会话模式持久化与时间间隔提醒
# ---------------------------------------------------------------------------

def test_time_gap_message_recent_returns_none():
    assert build_time_gap_message(datetime.now(timezone.utc)) is None
    assert build_time_gap_message(
        datetime.now(timezone.utc) - timedelta(hours=2)
    ) is None


def test_time_gap_message_old_returns_reminder():
    old = datetime.now(timezone.utc) - timedelta(days=3)
    msg = build_time_gap_message(old)
    assert msg is not None
    assert msg.role == "user"
    assert "3 days" in msg.content
    assert "re-read" in msg.content


def test_time_gap_message_handles_naive_datetime():
    naive_old = (
        datetime.now(timezone.utc) - timedelta(days=2)
    ).replace(tzinfo=None)
    msg = build_time_gap_message(naive_old)
    assert msg is not None
    assert "2 days" in msg.content


def test_session_meta_session_mode_roundtrip(tmp_path):
    meta = SessionMeta(id="sess-1", title="demo", session_mode="coordinator")
    path = tmp_path / "sess-1.meta"
    meta.save(path)

    loaded = SessionMeta.load(path)
    assert loaded is not None
    assert loaded.session_mode == "coordinator"


def test_session_meta_legacy_file_without_mode(tmp_path):
    legacy = {
        "id": "sess-2",
        "title": "old",
        "message_count": 3,
        "total_tokens": 100,
        "created_at": "2025-01-01T00:00:00+00:00",
        "last_active": "2025-01-01T00:00:00+00:00",
    }
    path = tmp_path / "sess-2.meta"
    path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = SessionMeta.load(path)
    assert loaded is not None
    assert loaded.session_mode == ""


def test_match_session_mode_enters_coordinator(monkeypatch):
    monkeypatch.delenv("MEWCODE_COORDINATOR_MODE", raising=False)

    note = match_session_mode("coordinator", enable_flag=True)

    assert note is not None
    assert "coordinator" in note
    assert os.environ.get("MEWCODE_COORDINATOR_MODE") == "1"
    monkeypatch.delenv("MEWCODE_COORDINATOR_MODE", raising=False)


def test_match_session_mode_exits_coordinator(monkeypatch):
    monkeypatch.setenv("MEWCODE_COORDINATOR_MODE", "1")

    note = match_session_mode("normal", enable_flag=True)

    assert note is not None
    assert "Exited" in note
    assert "MEWCODE_COORDINATOR_MODE" not in os.environ


def test_match_session_mode_noop_when_aligned(monkeypatch):
    monkeypatch.setenv("MEWCODE_COORDINATOR_MODE", "1")
    assert match_session_mode("coordinator", enable_flag=True) is None
    assert match_session_mode(None, enable_flag=True) is None
    assert match_session_mode("", enable_flag=True) is None
    monkeypatch.delenv("MEWCODE_COORDINATOR_MODE", raising=False)
