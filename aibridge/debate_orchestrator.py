import asyncio
import json
import sys
from .debate_state import DebateState
from .debate_prompts import (
    NORMALIZE_PROMPT, ARCHITECT_PROMPT, RED_TEAM_PROMPT,
    ALTERNATIVE_PROMPT, CRITIQUE_PROMPT, SYNTHESIS_PROMPT,
    DECISION_MATRIX_PROMPT
)
from .json_utils import parse_or_repair_json
from .report_writer import save_json_log, save_markdown_report

import uuid

class DebateOrchestrator:
    def __init__(self, bridge, events=None):
        self.bridge = bridge
        self.events = events

    def _new_node_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12]}"

    async def _query_model(self, prompt: str, source: str, node_id: str = None) -> str:
        text = ""
        try:
            first_token = True
            async for token in self.bridge.stream_completion(prompt, source=source):
                if first_token and not token.startswith("\0CLEAR\0"):
                    token = "\0CLEAR\0" + token
                first_token = False

                if token.startswith("\0CLEAR\0"):
                    text = token[7:]
                else:
                    text += token

                if self.events and node_id:
                    await self.events.emit_token(node_id, token)
            return text.strip()
        except Exception as e:
            print(f"\n[Ошибка при запросе {source}]: {e}", file=sys.stderr)
            return f"Error: {e}"

    async def _repair_bridge(self, prompt: str, source: str) -> str:
        return await self._query_model(prompt, source)

    async def run_debate(self, topic: str):
        state = DebateState(topic=topic)
        print(f"\n🚀 Запуск дебатов по теме: {topic}\n")

        user_node_id = self._new_node_id("user")
        if self.events:
            await self.events.emit_node_create(user_node_id, "user", title="User Request", content=topic)
            await self.events.emit_node_complete(user_node_id)

        # Round 0: Normalize Problem
        print("[*] Раунд 0: Формализация задачи (ChatGPT)...")
        draft_node_id = self._new_node_id("draft")
        if self.events:
            await self.events.emit_node_create(draft_node_id, "draft", title="Formalizing Problem", content="Thinking...")
            await self.events.emit_edge(user_node_id, draft_node_id, label="normalize")

        prompt_round0 = NORMALIZE_PROMPT.format(topic=topic)
        res_0 = await self._query_model(prompt_round0, "chatgpt", node_id=draft_node_id)
        state.raw_responses["round_0"] = res_0
        
        if self.events:
            await self.events.emit_node_complete(draft_node_id, content=res_0, status="complete")

        try:
            state.normalized_problem = await parse_or_repair_json(res_0, self._repair_bridge)
            print("    Успешно формализовано.")
        except Exception as e:
            print(f"    [!] Не удалось распарсить JSON формализации: {e}")
            state.normalized_problem = {"raw_fallback": res_0}

        norm_prob_text = json.dumps(state.normalized_problem, ensure_ascii=False, indent=2)

        # Round 1: Independent positions
        print("\n[*] Раунд 1: Сбор независимых позиций...")
        p_arch = ARCHITECT_PROMPT.format(normalized_problem=norm_prob_text)
        p_red = RED_TEAM_PROMPT.format(normalized_problem=norm_prob_text)
        p_alt = ALTERNATIVE_PROMPT.format(normalized_problem=norm_prob_text)

        arch_node_id = self._new_node_id("cloud_chatgpt")
        red_node_id = self._new_node_id("cloud_claude")
        alt_node_id = self._new_node_id("cloud_gemini")
        
        if self.events:
            await self.events.emit_node_create(arch_node_id, "cloudBrain", title="System Architect (ChatGPT)", content="Thinking...")
            await self.events.emit_edge(draft_node_id, arch_node_id, label="position")
            await self.events.emit_node_create(red_node_id, "cloudBrain", title="Red Team (Claude)", content="Thinking...")
            await self.events.emit_edge(draft_node_id, red_node_id, label="position")
            await self.events.emit_node_create(alt_node_id, "cloudBrain", title="Alternative Strategist (Gemini)", content="Thinking...")
            await self.events.emit_edge(draft_node_id, alt_node_id, label="position")

        t_arch = asyncio.create_task(self._query_model(p_arch, "chatgpt", node_id=arch_node_id))
        t_red = asyncio.create_task(self._query_model(p_red, "claude", node_id=red_node_id))
        t_alt = asyncio.create_task(self._query_model(p_alt, "gemini", node_id=alt_node_id))

        pos_arch, pos_red, pos_alt = await asyncio.gather(t_arch, t_red, t_alt)

        if self.events:
            await self.events.emit_node_complete(arch_node_id, content=pos_arch, status="complete")
            await self.events.emit_node_complete(red_node_id, content=pos_red, status="complete")
            await self.events.emit_node_complete(alt_node_id, content=pos_alt, status="complete")
        
        state.round_1_positions = {
            "chatgpt (Architect)": pos_arch,
            "claude (Red Team)": pos_red,
            "gemini (Alternative)": pos_alt
        }
        state.raw_responses["round_1"] = state.round_1_positions
        print("    Позиции получены.")

        # Round 2: Cross Critique
        print("\n[*] Раунд 2: Перекрестная критика...")
        positions_text = f"ChatGPT (Архитектор):\n{pos_arch}\n\nClaude (Критик):\n{pos_red}\n\nGemini (Альтернатива):\n{pos_alt}"
        
        p_critique = CRITIQUE_PROMPT.format(positions=positions_text)
        
        crit_arch_node = self._new_node_id("cloud_chatgpt_crit")
        crit_red_node = self._new_node_id("cloud_claude_crit")
        crit_alt_node = self._new_node_id("cloud_gemini_crit")
        
        if self.events:
            await self.events.emit_node_create(crit_arch_node, "cloudBrain", title="Critique (ChatGPT)", content="Thinking...")
            await self.events.emit_edge(arch_node_id, crit_arch_node, label="critique")
            await self.events.emit_node_create(crit_red_node, "cloudBrain", title="Critique (Claude)", content="Thinking...")
            await self.events.emit_edge(red_node_id, crit_red_node, label="critique")
            await self.events.emit_node_create(crit_alt_node, "cloudBrain", title="Critique (Gemini)", content="Thinking...")
            await self.events.emit_edge(alt_node_id, crit_alt_node, label="critique")

        t_crit_arch = asyncio.create_task(self._query_model(p_critique, "chatgpt", node_id=crit_arch_node))
        t_crit_red = asyncio.create_task(self._query_model(p_critique, "claude", node_id=crit_red_node))
        t_crit_alt = asyncio.create_task(self._query_model(p_critique, "gemini", node_id=crit_alt_node))

        crit_arch, crit_red, crit_alt = await asyncio.gather(t_crit_arch, t_crit_red, t_crit_alt)

        if self.events:
            await self.events.emit_node_complete(crit_arch_node, content=crit_arch, status="complete")
            await self.events.emit_node_complete(crit_red_node, content=crit_red, status="complete")
            await self.events.emit_node_complete(crit_alt_node, content=crit_alt, status="complete")

        state.round_2_critiques = {
            "chatgpt": crit_arch,
            "claude": crit_red,
            "gemini": crit_alt
        }
        state.raw_responses["round_2"] = state.round_2_critiques
        print("    Критика завершена.")

        print("\n[*] Раунд 3: Синтез решения (ChatGPT)...")
        synthesis_node_id = self._new_node_id("synthesis")
        if self.events:
            await self.events.emit_node_create(synthesis_node_id, "synthesis", title="Final Analysis", content="Synthesizing...")
            await self.events.emit_edge(crit_arch_node, synthesis_node_id, label="synthesize")
            await self.events.emit_edge(crit_red_node, synthesis_node_id, label="synthesize")
            await self.events.emit_edge(crit_alt_node, synthesis_node_id, label="synthesize")

        critiques_text = f"ChatGPT:\n{crit_arch}\n\nClaude:\n{crit_red}\n\nGemini:\n{crit_alt}"
        p_synth = SYNTHESIS_PROMPT.format(normalized_problem=norm_prob_text, positions=positions_text, critiques=critiques_text)
        
        synthesis = await self._query_model(p_synth, "chatgpt", node_id=synthesis_node_id)
        state.synthesis = synthesis
        state.raw_responses["round_3"] = synthesis
        print("    Синтез завершен.")

        # Round 4: Decision Matrix
        print("\n[*] Раунд 4: Оценка и Матрица Решений (Claude)...")
        if self.events:
            await self.events.emit_token(synthesis_node_id, "\n\n### Строим матрицу решений...\n")

        p_matrix = DECISION_MATRIX_PROMPT.format(synthesis=synthesis)
        matrix = await self._query_model(p_matrix, "claude", node_id=synthesis_node_id)
        state.decision_matrix = matrix
        state.raw_responses["round_4"] = matrix
        print("    Матрица построена.")

        if self.events:
            final_markdown = f"{synthesis}\n\n## Матрица решений\n\n{matrix}"
            await self.events.emit_node_complete(synthesis_node_id, content=final_markdown, status="complete")

        # Save Reports
        print("\n[*] Сохранение отчетов...")
        md_path = save_markdown_report(state)
        json_path = save_json_log(state)
        state.final_report_path = md_path

        print(f"\n✅ Дебаты завершены!")
        print(f"📄 Markdown отчет: {md_path}")
        print(f"💾 JSON лог: {json_path}")

async def run_debate_cli(topic: str, bridge):
    orchestrator = DebateOrchestrator(bridge)
    await orchestrator.run_debate(topic)
