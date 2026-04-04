# Implementation Tasks: ACP Provider for Honcho

## Overview

Add an ACP provider to Honcho that routes LLM calls through an external gateway, fix Conclusion schema bugs, and configure local embeddings. All tasks are complete (retrospective spec).

## Tasks

- [x] 1. Schema fixes for observation levels
  - [x] 1.1 Add `level` and `source_ids` fields to `Conclusion` and `ConclusionCreate` in `src/schemas/api.py`
    - _Requirements: 1.2, 1.3_
  - [x] 1.2 Fix hardcoded `level="explicit"` in `create_observations()` in `src/crud/document.py` to use provided level
    - _Requirements: 1.1_
  - [x] 1.3 Add `source_ids` passthrough in `create_observations()` (embedding and non-embedding paths) and `create_documents()` VectorRecord
    - _Requirements: 1.5, 1.6_

- [x] 2. ACP provider type and configuration
  - [x] 2.1 Add `"acp"` to `SupportedProviders` in `src/utils/types.py`
    - _Requirements: 2.1_
  - [x] 2.2 Add `ACP_GATEWAY_URL` and `ACP_TIMEOUT_MS` to `LLMSettings` in `src/config.py`
    - _Requirements: 2.2_
  - [x] 2.3 Register ACP client URL in `CLIENTS["acp"]` in `src/utils/clients.py`
    - _Requirements: 2.3_
  - [x] 2.4 Add ACP dispatch in `honcho_llm_call_inner()` before `match client:` block
    - _Requirements: 2.4_
  - [x] 2.5 Add ACP dispatch in `handle_streaming_response()` (non-streaming fallback, single chunk)
    - _Requirements: 2.5_

- [x] 3. ACP provider module (`src/utils/acp_provider.py`)
  - [x] 3.1 Implement `detect_module()` — module detection from tools and prompt keywords
    - _Requirements: 2.10_
  - [x] 3.2 Implement `build_agentic_prompt()` — combined prompt with system/task/tools sections
    - _Requirements: 2.9_
  - [x] 3.3 Implement `TOOL_NAME_MAP` — Honcho internal → canonical MCP name mapping
    - _Requirements: 2.11_
  - [x] 3.4 Implement `parse_deriver_response()` — JSON parsing with graceful fallback
    - _Requirements: 2.8_
  - [x] 3.5 Implement `AcpLLMResponse` dataclass compatible with `HonchoLLMCallResponse`
    - _Requirements: 2.12_
  - [x] 3.6 Implement `honcho_llm_call_inner_acp()` — main entry point with non-agentic and agentic paths
    - _Requirements: 2.6, 2.7, 2.8, 2.9, 2.13_

- [x] 4. Embedding configuration
  - [x] 4.1 Change default embedding model to `qwen3-embedding:0.6b` in `src/embedding_client.py`
    - _Requirements: 3.1_
  - [x] 4.2 Change vector dimensions from 1536 to 1024 in `src/models.py` (`MessageEmbedding` and `Document`)
    - _Requirements: 3.2_
