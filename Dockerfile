# The image the commits were labeled in: its Python and SQLite decide which
# sqlglot tests run, so local test runs here behave like the labeling run.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.lock /app/requirements.lock
RUN pip install -q -r /app/requirements.lock

# sqlglot at the pinned commit, straight from its GitHub repository.
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import io,tarfile,urllib.request; \
data=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz').read(); \
tarfile.open(fileobj=io.BytesIO(data)).extractall('/opt', filter='data')" \
 && mv /opt/sqlglot-${SQLGLOT_SHA} /opt/sqlglot \
 && rm -rf /opt/sqlglot/sqlglot-integration-tests /opt/sqlglot/posts /opt/sqlglot/benchmarks /opt/sqlglot/pdoc /opt/sqlglot/sqlglot.png

COPY blast /app/blast
COPY data /app/data

# Static index of the pristine sources, and bytecode for every module so the
# read-only runtime never compiles an unchanged file.
RUN cd /app && python -m blast.index /opt/sqlglot /app/data/index.pkl \
 && python -m compileall -q -j 4 /opt/sqlglot/sqlglot /opt/sqlglot/tests /app/blast \
 && chmod -R a+rX /app /opt/sqlglot

WORKDIR /app
CMD ["python", "-m", "blast.main"]
