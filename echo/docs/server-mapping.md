```markdown
# Model Infrastructure Documentation

Runtime note: the live orchestrator now reads machine-targetable instance settings from `/home/ubuntu/hack/instance-registry.json`. Keep this document as the human overview, and keep bridge URLs, SSH host/user, key path, and workspace paths in the JSON registry.

This document provides a detailed overview of the infrastructure supporting the Speech-to-Text (STT), Text-to-Speech (TTS), and orchestration pipeline.

---

## 🧠 System Overview

The system is divided into three primary components:

1. **STT Layer (Inference + Diarization)**
2. **TTS Layer (Speech Synthesis)**
3. **Orchestration Layer (Control + Routing + Backend Logic)**

Each component runs on a dedicated instance optimized for its workload.

---

## 1. 🗣️ STT-A10 (Speech-to-Text + Diarization)

**Host Alias:** `STT-A10`  
**Public DNS:** `ec2-3-108-184-58.ap-south-1.compute.amazonaws.com`  
**User:** `ubuntu`  

### 🎯 Purpose
Handles all incoming audio processing:
- Converts speech → text
- Performs speaker diarization (who spoke when)

### ⚙️ Workloads
- Real-time / batch STT inference
- Multi-speaker diarization pipelines

### 🧩 Services Running
- STT inference servers (likely GPU-backed)
- Diarization service (speaker segmentation + labeling)

### 🧠 Model Context
- Hosts ASR models (e.g., Whisper / custom models)
- Handles audio preprocessing (chunking, normalization)
- Outputs structured transcripts + speaker labels

### 🔗 Role in Pipeline
```

Audio Input → STT-A10 → Transcript + Speaker Segments → Hack Server

````

### 🔐 SSH Access
```bash
ssh -i "/Users/ahmadraza/WorkDir/professional/indusai/h100/h100.pem" ubuntu@ec2-3-108-184-58.ap-south-1.compute.amazonaws.com
````

---

## 2. 🔊 TTS-H100 (Text-to-Speech)

**Host Alias:** `TTS-H100`
**Public DNS:** `ec2-3-109-4-6.ap-south-1.compute.amazonaws.com`
**User:** `ubuntu`

### 🎯 Purpose

Generates speech from text outputs.

### ⚙️ Workloads

* High-performance TTS inference
* Likely optimized for GPU-heavy synthesis (H100)

### 🧩 Services Running

* TTS inference servers
* Voice synthesis pipelines

### 🧠 Model Context

* Hosts TTS models (e.g., neural vocoders, transformer-based models)
* Handles:

  * Text normalization
  * Voice generation
  * Audio waveform synthesis

### 🔗 Role in Pipeline

```
Processed Text → TTS-H100 → Generated Speech Output
```

### 🔐 SSH Access

```bash
ssh -i "h100.pem" ubuntu@ec2-3-109-4-6.ap-south-1.compute.amazonaws.com
```

---

## 3. 🧩 Hack Server (Orchestration + Backend)

**Host Alias:** `hack`
**Public IP:** `13.201.179.146`
**User:** `ubuntu`

### 🎯 Purpose

Acts as the **central brain** of the system:

* Orchestrates requests between STT and TTS
* Handles backend logic and workflows

### ⚙️ Responsibilities

* API layer / request handling
* Routing:

  * Audio → STT-A10
  * Text → TTS-H100
* Aggregation of outputs
* Business logic / pipeline control

### 🧩 Services Running

* Orchestration services
* Backend APIs
* Possibly:

  * Queue systems (Redis, Kafka, etc.)
  * Task schedulers
  * Session/state management

### 🧠 Model Context

* Does NOT run heavy models directly
* Coordinates model usage across instances
* Maintains flow consistency and response structure

### 🔗 Role in Pipeline

```
Client Request
     ↓
Hack Server (Orchestrator)
     ↓
STT-A10 (if audio input)
     ↓
TTS-H100 (if speech output needed)
     ↓
Final Response to Client
```

### 🔐 SSH Access

```bash
ssh -i "/Users/ahmadraza/WorkDir/professional/indusai/h100/h100.pem" ubuntu@13.201.179.146
```

---

## 🔄 End-to-End Flow

### 🎙️ Speech-to-Speech Pipeline

```
User Audio
   ↓
Hack Server
   ↓
STT-A10 (Transcription + Diarization)
   ↓
Hack Server (Processing / Logic)
   ↓
TTS-H100 (Speech Generation)
   ↓
Final Audio Output
```

### 💬 Text-to-Speech Only

```
Input Text
   ↓
Hack Server
   ↓
TTS-H100
   ↓
Audio Output
```

---

## 📊 Summary Table

| Instance | Role                    | Key Functions                   |
| -------- | ----------------------- | ------------------------------- |
| STT-A10  | Speech Processing       | STT + Diarization               |
| TTS-H100 | Speech Generation       | TTS Inference                   |
| Hack     | Orchestration / Backend | Routing, APIs, Pipeline Control |
