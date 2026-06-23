# SmartCare QA Assistant - Local File Upload (+) And RAG Integration Steps

## 1. Objective

Enable a + button in chat to upload local files and use those files as retrieval context (RAG) during chat responses.

Expected outcome:
- User uploads files from local machine.
- Files are parsed, sanitized, chunked, embedded, and indexed.
- Chat answers are grounded using retrieved chunks from uploaded files.

---

## 2. Current Baseline In Code

Observed in current implementation:
- Chat send flow exists and posts to /api/chat.
- Placeholder file UI exists in keyword section, but no real upload ingestion for chat.
- AI Search storage package exists, but search client implementation is empty.

This means upload + RAG requires both frontend and backend implementation.

---

## 3. Frontend Changes (Point By Point)

### 3.1 Add + Upload Control In Chat Composer

In chat composer area:
1. Add a visible + button.
2. Add a hidden input type=file.
3. Set multiple attribute for multi-file upload.
4. Restrict accepted file types (for example: .pdf, .docx, .txt, .md, .csv, .json).

Example behavior:
- Click + -> open local file picker.
- On selection -> upload starts.

### 3.2 Add Selected Files UI

1. Add a small panel below composer for selected/attached files.
2. For each file show:
   - File name
   - Size
   - Status (Uploading, Indexed, Failed)
   - Remove action
3. Keep a client-side array for current attached document ids.

### 3.3 Add Upload API Call

1. Create FormData in JavaScript.
2. Append one or many files.
3. POST to /api/rag/upload.
4. Read returned document ids and chunk stats.
5. Persist ids in the active chat session state.

### 3.4 Extend sendChat Payload

Current request body has message only. Extend to include:
- message
- uploaded_doc_ids (array)
- session_id (optional but recommended)

When user sends message:
1. Include uploaded_doc_ids in /api/chat request.
2. Disable send during request.
3. Re-enable after response.

### 3.5 Chat UX And Error Handling

1. Show upload progress and failure reasons inline.
2. Block unsupported file types with user-friendly message.
3. Add retry upload action for failed files.
4. Optionally keep uploaded files pinned to chat session until removed.

### 3.6 Optional UI Enhancements

1. Add Knowledge Sources section in sidebar.
2. Show active documents and total chunk count.
3. Add detach/delete document action.

---

## 4. Backend Changes (Point By Point)

### 4.1 Extend Request Models

Update chat request model with:
- uploaded_doc_ids: list of strings (default empty)
- session_id: optional string

### 4.2 Add Upload Endpoint

Add new API route:
- POST /api/rag/upload

Responsibilities:
1. Accept multipart files.
2. Validate file type and size.
3. Parse text content by file type.
4. Run PHI sanitization before indexing.
5. Chunk text.
6. Generate embeddings.
7. Store chunks in vector index.
8. Return document ids and indexing summary.

### 4.3 Add Retrieval Service Function

Add retrieval function (internal or API):
- Input: query text, uploaded_doc_ids, top_k
- Output: top relevant chunks with metadata (doc name, chunk id, score)

### 4.4 Integrate RAG In Chat Endpoint

In /api/chat flow:
1. If uploaded_doc_ids present:
   - Retrieve top-k chunks from AI Search index.
   - Build compact grounded context block.
   - Pass context into FoundryClient.generate.
2. If no uploaded docs:
   - Keep existing ADO/general routing behavior unchanged.

### 4.5 Implement AI Search Client

Implement search client module with:
1. ensure_index()
2. upsert_chunks(document_id, chunks, metadata)
3. vector_search(query_embedding, filter_doc_ids, top_k)
4. delete_document(document_id) (optional)

### 4.6 Add File Parsers

Add parser layer for allowed types:
- PDF parser
- DOCX parser
- Plain text/markdown/csv/json readers

Normalization:
1. Convert all parsed content to UTF-8 text.
2. Remove invalid characters.
3. Preserve section headings when possible.

### 4.7 Add Chunking Strategy

Recommended baseline:
- Chunk size: 800 to 1200 characters
- Overlap: 150 to 200 characters

Store per chunk:
- document_id
- filename
- chunk_id
- chunk_text
- char_count
- upload_timestamp
- session_id

### 4.8 Add Config Settings

Add settings for:
- AI_SEARCH_ENDPOINT
- AI_SEARCH_KEY
- AI_SEARCH_INDEX_NAME
- EMBEDDING_MODEL
- MAX_UPLOAD_MB
- ALLOWED_UPLOAD_TYPES
- RAG_TOP_K

### 4.9 Add Dependencies

Update requirements with selected libraries such as:
- azure-search-documents
- pypdf
- python-docx
- optional tokenizer utility

### 4.10 Add Security And Compliance Controls

1. Enforce size/type validation server-side.
2. Reject executable and unsafe formats.
3. Sanitize PHI before embedding/indexing.
4. Log all upload/retrieval events for audit.
5. Apply RBAC for upload and retrieval endpoints if required.

---

## 5. API Contracts

### 5.1 Upload Files

Endpoint:
- POST /api/rag/upload

Request:
- multipart form-data
- files[]
- optional session_id

Response:
- uploaded_documents: [{document_id, filename, chunks_indexed}]
- total_chunks
- warnings

### 5.2 Chat With RAG Context

Endpoint:
- POST /api/chat

Request JSON:
- message
- uploaded_doc_ids
- session_id

Response JSON:
- assistant_message
- response_meta: {rag_used, retrieved_sources_count, sources}

### 5.3 Optional Document Delete

Endpoint:
- DELETE /api/rag/document/{document_id}

Purpose:
- Remove document chunks and unlink from session.

---

## 6. End-To-End Runtime Sequence

1. User clicks + and selects files.
2. Frontend posts files to /api/rag/upload.
3. Backend validates and parses files.
4. Backend sanitizes PHI.
5. Backend chunks text and generates embeddings.
6. Backend indexes chunks in Azure AI Search.
7. Backend returns document ids.
8. User asks a chat question.
9. Frontend calls /api/chat with uploaded_doc_ids.
10. Backend retrieves top relevant chunks.
11. Backend sends grounded context to LLM.
12. Assistant returns answer with source metadata.

---

## 7. Data Stores Needed

1. Vector index (Azure AI Search)
- Chunk embeddings and metadata.

2. Optional relational store (SQL/Cosmos)
- Upload session metadata.
- Document ownership and lifecycle.

3. Blob storage (optional)
- Raw file archival if policy requires retention.

---

## 8. Reports And Telemetry To Generate

### 8.1 Operational Metrics

- Upload success/failure rate
- Average indexing latency
- Average retrieval latency
- Chunks per document

### 8.2 RAG Quality Metrics

- Retrieval hit count per query
- Source utilization frequency
- User feedback signals for groundedness

### 8.3 Security Metrics

- PHI sanitization event counts
- Rejected file attempts by policy
- Audit trail completeness

---

## 9. Recommended Phased Rollout

Phase 1 (MVP):
1. + upload button in chat
2. Single file type support (txt/md/pdf)
3. Basic upload, chunk, index, retrieve
4. Chat grounding with source list

Phase 2:
1. Add docx/csv/json parsers
2. Multi-file upload and per-session persistence
3. Better ranking and reranking

Phase 3:
1. Document lifecycle APIs (delete/expire)
2. Advanced observability dashboards
3. Governance and RBAC hardening

---

## 10. Implementation Checklist

Frontend checklist:
- Add + button and hidden file input
- Add selected files panel and statuses
- Integrate /api/rag/upload call
- Extend /api/chat payload with uploaded_doc_ids
- Handle errors, retries, and disabled states

Backend checklist:
- Add upload endpoint
- Add parser and sanitizer pipeline
- Add chunking and embedding pipeline
- Implement AI Search client functions
- Integrate retrieval in chat path
- Add configs, validation, and audit logs

Testing checklist:
- Upload valid and invalid file types
- Verify PHI sanitization path
- Verify retrieval uses only attached docs
- Verify chat response includes grounded sources
- Verify no regression in existing ADO-grounded chat behavior

---

## 11. Final Notes

This approach keeps your existing ADO-focused assistant behavior intact while adding document-grounded RAG as an optional capability. The + upload workflow can coexist with current prompts and progressively expand from MVP to enterprise-grade implementation.
