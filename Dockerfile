# Blast Radius selector. Same base image the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock

# sqlglot at the pinned commit (GitHub source archive of that exact commit)
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import io,tarfile,urllib.request,os,shutil; \
d=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz').read(); \
tarfile.open(fileobj=io.BytesIO(d)).extractall('/app', filter='data'); \
os.rename('/app/sqlglot-${SQLGLOT_SHA}', '/app/repo')"

COPY selector /app/selector
COPY tools/make_stages.py /app/tools/make_stages.py
RUN python -m compileall -q -j 0 /app/repo/sqlglot /app/repo/tests /app/selector \
 && cd / && python /app/tools/make_stages.py /app/repo /app/stages.json \
 && chmod -R a+rX /app

ENV BR_REPO=/app/repo BR_STAGES=/app/stages.json
CMD ["python", "/app/selector/main.py"]
