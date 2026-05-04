"""Session log, chronicle note, and calendar tools."""
import re
from pathlib import Path
from datetime import date
import _campaign as _c


def register(mcp):

    @mcp.tool()
    def log_note(text: str) -> dict:
        """Append a bullet note to the current session in the chronicle markdown file.
        Finds the last '### Session' heading and appends the note before the next heading or EOF."""
        cfg = _c.load_campaign()
        chronicle_path = Path(cfg["_data_dir"]) / cfg.get("session_log_file", "adventure_log.md")

        chronicle_path.parent.mkdir(parents=True, exist_ok=True)

        if not chronicle_path.exists():
            chronicle_path.write_text("")

        content = chronicle_path.read_text(encoding="utf-8")
        bullet = f"- {text}\n"

        # Find last ### Session heading
        matches = list(re.finditer(r'^### Session', content, re.MULTILINE))

        if not matches:
            # No session yet — just append
            if content and not content.endswith("\n"):
                content += "\n"
            content += bullet
        else:
            last_match = matches[-1]
            after_session = content[last_match.start():]

            # Find the next heading after the last session
            next_heading = re.search(r'\n(?=#{1,3} )', after_session[1:])
            if next_heading:
                insert_pos = last_match.start() + 1 + next_heading.start()
                # Ensure there's a blank line before the next heading
                before = content[:insert_pos]
                after = content[insert_pos:]
                if not before.endswith("\n"):
                    before += "\n"
                content = before + bullet + after
            else:
                # Append at end
                if not content.endswith("\n"):
                    content += "\n"
                content += bullet

        chronicle_path.write_text(content, encoding="utf-8")
        return {"appended": bullet.rstrip()}

    @mcp.tool()
    def new_session(title: str) -> dict:
        """Append a new session heading to the chronicle and increment the session counter."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        chronicle_path = Path(cfg["_data_dir"]) / cfg.get("session_log_file", "adventure_log.md")
        chronicle_path.parent.mkdir(parents=True, exist_ok=True)

        session_num = state.get("current_session", 0) + 1
        state["current_session"] = session_num
        _c.save_state(cfg, state)

        today = date.today().strftime("%Y-%m-%d")
        heading = f"\n### Session {session_num} — {title}\n*{today}*\n\n"

        if not chronicle_path.exists():
            chronicle_path.write_text(heading.lstrip(), encoding="utf-8")
        else:
            with chronicle_path.open("a", encoding="utf-8") as f:
                f.write(heading)

        _c.append_event(cfg, {"type": "session_start", "session": session_num, "title": title, "date": today})

        return {
            "session": session_num,
            "title": title,
        }

    @mcp.tool()
    def advance_calendar(days: int) -> dict:
        """Advance the in-game calendar by a number of days."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)

        current = state.get("current_day", 1)
        new_day = current + days
        state["current_day"] = new_day
        _c.save_state(cfg, state)

        return {
            "days_advanced": days,
            "new_day": new_day,
        }
