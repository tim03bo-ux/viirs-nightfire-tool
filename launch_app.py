"""
Launcher for the VIIRS Nightfire dashboard as a standalone executable.
Starts Streamlit programmatically and opens the browser.
"""

import sys
import os

# Must set these BEFORE any streamlit import
os.environ['STREAMLIT_BROWSER_GATHER_USAGE_STATS'] = 'false'
os.environ['STREAMLIT_SERVER_HEADLESS'] = 'false'
os.environ['STREAMLIT_GLOBAL_DEVELOPMENT_MODE'] = 'false'
os.environ['STREAMLIT_SERVER_PORT'] = '8501'


def get_app_dir():
    """Return the directory containing the bundled data files."""
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def get_exe_dir():
    """Return the directory where the exe lives (for user-editable files like .env)."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_env():
    """Load .env file into environment variables. Checks exe dir first, then bundle."""
    for base in [get_exe_dir(), get_app_dir()]:
        env_path = os.path.join(base, '.env')
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if '=' in line and not line.startswith('#'):
                        key, value = line.split('=', 1)
                        os.environ.setdefault(key.strip(), value.strip())
            return


def main():
    load_env()

    app_dir = get_app_dir()
    dashboard_path = os.path.join(app_dir, 'dashboard.py')

    # Ensure our src package is importable
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)

    # Force developmentMode off via Streamlit's config system
    import streamlit.config as config
    config.set_option('global.developmentMode', False)
    config.set_option('server.port', 8501)
    config.set_option('server.headless', False)
    config.set_option('browser.gatherUsageStats', False)

    from streamlit.web import bootstrap
    bootstrap.run(
        main_script_path=dashboard_path,
        is_hello=False,
        args=[],
        flag_options={
            'server.port': 8501,
            'server.headless': False,
            'browser.gatherUsageStats': False,
            'global.developmentMode': False,
        },
    )


if __name__ == '__main__':
    main()
