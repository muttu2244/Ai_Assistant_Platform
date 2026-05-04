FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1 \
	PIP_NO_CACHE_DIR=1

WORKDIR /app

# System dependencies for building Python wheels (e.g., pyodbc) and runtime ODBC support.
RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
	   build-essential \
	   gcc \
	   g++ \
	   curl \
	   unixodbc \
	   unixodbc-dev \
	&& rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./

RUN python -m pip install --upgrade pip setuptools wheel \
	&& pip install -r requirements.txt

# Install NLP model assets required by local Presidio mode.
ARG SPACY_MODEL=en_core_web_lg
RUN python -m spacy download ${SPACY_MODEL} \
	&& python -c "import spacy; spacy.load('${SPACY_MODEL}')"

COPY . .

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "src.chat_ui.app:app", "--host", "0.0.0.0", "--port", "8000"]
