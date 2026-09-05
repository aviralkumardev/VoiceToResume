from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "Hiring Platform"

    openrouter_api_key: str
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_default_model: str = "openai/gpt-5.6-luna:nitro"
    default_temperature: float = 0.7
    llm_request_timeout_seconds: int = 120

    system_prompt_caching_enabled: bool = True
    max_json_repair_retries: int = 3
    max_rate_limit_retries: int = 3
    rate_limit_backoff_base_seconds: float = 1.0

    sarvam_api_key: str
    daily_api_key: str
    openai_api_key: str

    resume_room_daily_worker_threads: int = 2
    resume_room_expiry_seconds: int = 3600
    resume_room_max_session_seconds: int = 3600
    resume_room_bot_name: str = "AI Resume Expert"

    resume_room_stt_provider: str = "sarvam"
    resume_room_tts_provider: str = "sarvam"

    resume_room_extraction_trigger_chars: int = 100
    resume_room_extraction_max_carry_multiple: int = 4

    resume_room_combined_provider: str = "openai"
    resume_room_combined_model: str = "gpt-5.6-terra"
    resume_room_combined_max_tokens: int = 8000
    resume_room_combined_reasoning_effort: str = "none"

    resume_room_final_pass_provider: str = "openrouter"
    resume_room_final_pass_model: str = "openai/gpt-5.6-terra:nitro"
    resume_room_final_pass_max_tokens: int = 4000

    resume_room_silence_hardbound_seconds: float = 0.5
    resume_room_answer_silence_seconds: float = 0.5

    resume_room_question_provider: str = "openai"
    resume_room_question_model: str = "gpt-5.6-terra"
    resume_room_question_max_tokens: int = 3000
    resume_room_question_reasoning_effort: str = "none"

    resume_room_max_questions_per_round: int = 2

    resume_room_min_evidence_tokens: int = 3
   
    resume_room_flush_timeout_seconds: float = 8.0

    resume_room_sarvam_stt_model: str = "saaras:v3"
    resume_room_sarvam_tts_model: str = "bulbul:v3"
    resume_room_sarvam_tts_speaker: str = "shubh"
    resume_room_sarvam_language: str = "en-IN"

    resume_room_max_sessions: int = 3
    resume_room_max_participants_per_session: int = 2
    resume_room_empty_room_grace_seconds: int = 15
    resume_room_idle_timeout_seconds: int = 300

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
