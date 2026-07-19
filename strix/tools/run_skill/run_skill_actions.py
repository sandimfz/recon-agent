import os
from typing import Any

from strix.tools.registry import register_tool


@register_tool
def execute_skill(
    skill_name: str,
    script_name: str,
    arguments: str | None = None,
) -> dict[str, Any]:
    """
    Execute a specialized security skill script from the skills library.
    """
    from strix.tools.terminal.terminal_manager import get_terminal_manager

    manager = get_terminal_manager()

    # Scripts are synced to /workspace/skills/ in the sandbox
    script_path = f"/workspace/skills/custom/{skill_name}/scripts/{script_name}"
    
    # Check if file exists in sandbox first
    check_command = f"ls {script_path}"
    check_result = manager.execute_command(check_command)
    
    if check_result.get("exit_code") != 0:
        return {
            "success": False,
            "error": f"Script '{script_name}' not found for skill '{skill_name}' at {script_path}.",
            "output": check_result.get("content", "")
        }

    # Determine interpreter
    if script_name.endswith(".py"):
        cmd = f"python3 {script_path}"
    elif script_name.endswith(".sh"):
        cmd = f"bash {script_path}"
    else:
        cmd = script_path # Assume executable

    if arguments:
        cmd += f" {arguments}"

    try:
        execution_result = manager.execute_command(cmd)
        return {
            "success": execution_result.get("exit_code") == 0,
            "skill": skill_name,
            "script": script_name,
            "exit_code": execution_result.get("exit_code"),
            "output": execution_result.get("content", ""),
            "working_dir": execution_result.get("working_dir")
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "skill": skill_name,
            "script": script_name
        }
