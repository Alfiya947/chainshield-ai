# ChainShield AI (enhanced prototype)

Agentic refund-investigation platform: an agent investigates a request with several tools, re-plans after every
step, explains the result in plain language, asks an authorised human to approve consequential actions, and then
re-reads the ledger to prove the action worked. All data is synthetic.

## Run (Codespaces or local)
```bash
pip install -r requirements.txt
python -m streamlit run app.py --server.address 0.0.0.0
```
Run the automated tests: `python test_agent.py`

## Quick demo
1. Open **Workflow Simulator**, choose an example, and select **Analyze selected workflow**.
2. Read the SAFE, REVIEW, or BLOCKED result. Open **Technical evidence** for the check details.
3. Use **Next step** to try the approval gate and verified action flow.
4. Open **Test Lab** to run all 10 examples and 17 additional checks. **Audit Logs** searches and exports saved events.

The app also includes **Dashboard**, **Security Analysis**, **Dependency Graph**, and **Settings**. The simulator checks one refund request at a time; it does not run chained downstream actions.

## Voice briefing (ElevenLabs)
Open **Listen to this result** to hear the plain-English summary.
```bash
export ELEVENLABS_API_KEY=your_key      # in Codespaces: add it as a repository secret instead
```
Optional: `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL_ID`. Without a key, or if the call fails (bad key, no credits), the button
falls back to the browser's built-in voice, so the demo still speaks. Audio is cached per script in `.audio_cache/`.
Voice only reads findings the engine already produced and never changes a decision. Voice approval is deliberately not supported.

## Ask the agent (voice or text)
Open **Ask a question** to ask "Why was this flagged?", "What is missing?",
"What do you recommend?", "How risky is it?" and similar, by suggested button, typing, or recording (ElevenLabs speech-to-text).
Answers are read aloud (ElevenLabs, or browser voice as fallback).
- Typed questions work with no key at all. Recording needs `ELEVENLABS_API_KEY` and a Streamlit version with `st.audio_input` (1.39+).
- Answers are built only from the finished case report. The optional AI narrator toggle may rephrase safe answers; if it fails the rule-based answer is used.
- Requests to act ("approve it", "ignore the rules", "go ahead") are always refused. "Is this fraud?" is always answered with what is and is not proven.
- Every question is written to the case's audit trail. Questions never change a decision.
- Optional env var: `ELEVENLABS_STT_MODEL_ID` (default `scribe_v2`; the docs also list `scribe_v1`).

## Files
- `agent_engine.py` tools, agent loop with re-assessment, deterministic decision engine, risk score, transaction relationship graph, guarded actions
- `database.py` SQLite: history, approval register, cases, audit events, simulated ledger, persisted dashboard metrics
- `scenarios.py` demo cases (also used as tests) · `testsuite.py` shared checks · `test_agent.py` unit tests
- `voice.py` spoken briefing + speech-to-text (ElevenLabs, with browser-voice fallback)
- `qa.py` grounded question answering with action/fraud guardrails
- `llm_assist.py` optional AI narrator / planner hook (set `ANTHROPIC_API_KEY`); off by default
- `.streamlit/config.toml` dark theme

## Honest limitations
- The planner is rule-based (it adapts using findings, but is not an LLM). `investigate_transaction(proposer=...)` is the hook for an LLM planner; its suggestions are validated against the tool list.
- The AI narrator only writes explanations; it never makes or changes a decision. It has not been tested against the live API. The same applies to the ElevenLabs call: the request format is unit-tested with a mock, but it has not been run against the live service.
- Risk score is a triage priority built from fixed points, not a fraud probability. Tests prove designed behaviour, not real-world accuracy.
