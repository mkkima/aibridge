import os
import json
import re
from datetime import datetime

REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")

def ensure_reports_dir():
    if not os.path.exists(REPORTS_DIR):
        os.makedirs(REPORTS_DIR)

def generate_slug(text: str) -> str:
    """Generate a clean filename slug from the topic."""
    # Take first 40 chars to avoid excessively long filenames
    slug = text[:40].lower()
    # Replace non-alphanumeric (including russian chars) with underscore
    slug = re.sub(r'[^\w\sа-яА-ЯёЁ]', '', slug)
    slug = re.sub(r'\s+', '_', slug)
    return slug.strip('_')

def save_json_log(state, mode="debate") -> str:
    ensure_reports_dir()
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    slug = generate_slug(state.topic if hasattr(state, "topic") else state.get("topic", ""))
    filename = f"{timestamp}_{mode}_{slug}.json" if slug else f"{timestamp}_{mode}.json"
    filepath = os.path.join(REPORTS_DIR, filename)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        # Check if state has to_dict
        data = state.to_dict() if hasattr(state, "to_dict") else state
        json.dump(data, f, indent=2, ensure_ascii=False)
        
    return filepath

def format_ai_quote(text: str) -> str:
    """Properly formats AI output with blockquotes."""
    if not text:
        return ""
    return "\n".join(f"> {line}" for line in text.split("\n"))

def save_markdown_report(state) -> str:
    ensure_reports_dir()
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    slug = generate_slug(state.topic)
    filename = f"{timestamp}_debate_{slug}.md" if slug else f"{timestamp}_debate.md"
    filepath = os.path.join(REPORTS_DIR, filename)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        # YAML Frontmatter
        f.write("---\n")
        f.write(f"title: \"{state.topic.replace('\"', '')}\"\n")
        f.write(f"date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("type: debate\n")
        f.write("---\n\n")
        
        f.write(f"# ⚖️ Дебаты: {state.topic}\n\n")
        
        f.write("## 🎯 1. Формализация проблемы\n\n")
        f.write("<details>\n<summary>Посмотреть структуру (JSON)</summary>\n\n")
        f.write("```json\n")
        f.write(json.dumps(state.normalized_problem, indent=2, ensure_ascii=False) if state.normalized_problem else "Не удалось формализовать.")
        f.write("\n```\n")
        f.write("</details>\n\n")
        f.write("---\n\n")
        
        f.write("## 💡 2. Позиции (Раунд 1)\n\n")
        for source, pos in state.round_1_positions.items():
            f.write(f"### 👤 {source.upper()}\n\n")
            f.write(f"{format_ai_quote(pos)}\n\n")
        f.write("---\n\n")
            
        f.write("## ⚔️ 3. Перекрестная критика (Раунд 2)\n\n")
        for source, crit in state.round_2_critiques.items():
            f.write(f"### 🛡️ {source.upper()}\n\n")
            f.write(f"{format_ai_quote(crit)}\n\n")
        f.write("---\n\n")
            
        f.write("## 🧠 4. Синтез (Раунд 3)\n\n")
        f.write(f"{state.synthesis}\n\n")
        f.write("---\n\n")
        
        f.write("## 📋 5. Матрица решений (Раунд 4)\n\n")
        f.write(f"{state.decision_matrix}\n\n")
        
    return filepath

def save_parallel_report(topic: str, responses: dict, synthesis: str) -> str:
    ensure_reports_dir()
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    slug = generate_slug(topic)
    filename = f"{timestamp}_parallel_{slug}.md" if slug else f"{timestamp}_parallel.md"
    filepath = os.path.join(REPORTS_DIR, filename)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        # YAML Frontmatter
        f.write("---\n")
        f.write(f"title: \"{topic.replace('\"', '')}\"\n")
        f.write(f"date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("type: parallel_consultation\n")
        f.write("---\n\n")
        
        f.write(f"# ⚡ Параллельная Консультация: {topic}\n\n")
        f.write("---\n\n")
        
        f.write("## 🧠 Итоговая Сводка (Synthesis)\n\n")
        f.write(f"{synthesis}\n\n")
        f.write("---\n\n")
        
        f.write("## 💬 Ответы консультантов\n\n")
        for source, text in responses.items():
            f.write(f"<details>\n<summary><b>Развернуть ответ {source.upper()}</b></summary>\n\n")
            f.write(f"{format_ai_quote(text)}\n\n")
            f.write("</details>\n\n")
        
    # Save a JSON log too
    json_state = {
        "topic": topic,
        "mode": "parallel",
        "responses": responses,
        "synthesis": synthesis,
        "timestamp": timestamp
    }
    save_json_log(json_state, mode="parallel")
    
    return filepath
