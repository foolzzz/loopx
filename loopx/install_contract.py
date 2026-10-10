NO_CLONE_INSTALL_URL = "https://raw.githubusercontent.com/michaelx1993/loopx/main/install.sh"

DEFAULT_INSTALL_COMMAND = "curl -fsSL https://raw.githubusercontent.com/michaelx1993/loopx/main/install.sh | bash"
DEFAULT_WORKFLOW_SKILL_INSTALL_COMMAND = "loopx workflow-skills --install"
DEFAULT_INSTALL_REPAIR_COMMAND = (
    f"{DEFAULT_INSTALL_COMMAND}\n"
    f"{DEFAULT_WORKFLOW_SKILL_INSTALL_COMMAND}\n"
    "loopx doctor"
)
ARCHIVE_FALLBACK_INSTALL_COMMAND = (
    f"curl -fsSL {NO_CLONE_INSTALL_URL} | bash\n"
    'export PATH="$HOME/.local/bin:$PATH"\n'
    "loopx doctor"
)
