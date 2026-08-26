# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com

from __future__ import annotations

from typing import TYPE_CHECKING

from mewcode.commands.registry import Command, CommandContext, CommandType

if TYPE_CHECKING:
    from mewcode.skills.loader import SkillLoader


async def handle_skill(ctx: CommandContext) -> None:
    parts = ctx.args.strip().split(maxsplit=1)
    subcmd = parts[0] if parts else "list"
    sub_args = parts[1] if len(parts) > 1 else ""

    loader: SkillLoader | None = ctx.config.get("skill_loader")
    if loader is None:
        ctx.ui.add_system_message("Skill 系统未初始化")
        return

    if subcmd == "list":
        _handle_list(ctx, loader)
    elif subcmd == "info":
        _handle_info(ctx, loader, sub_args)
    elif subcmd == "reload":
        await _handle_reload(ctx, loader)
    elif subcmd == "install":
        await _handle_install(ctx, loader, sub_args)
    else:
        ctx.ui.add_system_message(
            f"未知子命令：{subcmd}\n"
            "用法：/skill list | /skill info <name> | /skill reload | "
            "/skill install <git-url|https-url.md|本地路径> [user|project]"
        )


def _handle_list(ctx: CommandContext, loader: SkillLoader) -> None:
    catalog = loader.get_catalog()
    if not catalog:
        ctx.ui.add_system_message("没有已加载的 Skill")
        return

    lines = ["已加载的 Skill："]
    for name, desc in catalog:
        source = loader.get_source_label(name)
        lines.append(f"  {name:<20} {desc}  [{source}]")
    ctx.ui.add_system_message("\n".join(lines))


def _handle_info(ctx: CommandContext, loader: SkillLoader, name: str) -> None:
    if not name:
        ctx.ui.add_system_message("用法：/skill info <name>")
        return

    skill = loader.get(name)
    if skill is None:
        ctx.ui.add_system_message(f"未找到 Skill：{name}")
        return

    source = loader.get_source_label(name)
    lines = [
        f"Skill: {skill.name}",
        f"Description: {skill.description}",
        f"Mode: {skill.mode}",
        f"Context: {skill.context}",
        f"Model: {skill.model or '(default)'}",
        f"AllowedTools: {', '.join(skill.allowed_tools) or '(all)'}",
        f"Source: {source}",
        f"Path: {skill.source_path or '(builtin)'}",
        f"Directory: {skill.is_directory}",
    ]
    ctx.ui.add_system_message("\n".join(lines))


async def _handle_reload(ctx: CommandContext, loader: SkillLoader) -> None:
    skills = loader.reload()

    registry = ctx.config.get("registry")
    if registry is not None:
        from mewcode.commands.handlers.skill_register import register_skill_commands
        register_skill_commands(registry, loader, ctx.config.get("skill_executor"))

    ctx.ui.add_system_message(f"已重新加载 {len(skills)} 个 Skill")


async def _handle_install(ctx: CommandContext, loader: SkillLoader, args: str) -> None:
    from mewcode.skills.installer import SkillInstallError, install_skill, make_install_summary

    parts = args.strip().split()
    if not parts:
        ctx.ui.add_system_message(
            "用法：/skill install <git-url|https-url.md|本地路径> [user|project]\n\n"
            "  - git 仓库：克隆后安装其中所有 SKILL.md（根目录或一级子目录）\n"
            "  - https 直链：下载 .md 文件并安装\n"
            "  - 本地路径：复制文件或目录\n"
            "  - scope 缺省 user（~/.mewcode/skills），可选 project（.mewcode/skills）"
        )
        return

    source = parts[0]
    scope = parts[1] if len(parts) > 1 else "user"

    ctx.ui.add_system_message(f"正在安装 Skill：{source} ...")
    try:
        results = await install_skill(source, loader, scope=scope)
    except SkillInstallError as e:
        ctx.ui.add_system_message(f"安装失败：{e}")
        return

    ctx.ui.add_system_message(make_install_summary(results))

    # 安装新 skill 后同步刷新斜杠命令注册（与 /skill reload 一致）
    registry = ctx.config.get("registry")
    if registry is not None:
        from mewcode.commands.handlers.skill_register import register_skill_commands
        register_skill_commands(registry, loader, ctx.config.get("skill_executor"))


SKILL_COMMAND = Command(
    name="skill",
    description="管理 Skill 技能包",
    usage="/skill list | /skill info <name> | /skill reload | /skill install <source> [scope]",
    type=CommandType.LOCAL,
    handler=handle_skill,
    aliases=["skills"],
)
