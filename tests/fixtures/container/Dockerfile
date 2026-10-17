FROM alpine:3.24@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b AS compiler-fixture
COPY main.go /source.go
RUN --mount=type=cache,target=/fixture/.next/cache,sharing=locked \
    mkdir -p /fixture/.next/cache/turbopack /fixture/.next/cache/fetch-cache \
    && cp /source.go /fixture/.next/cache/turbopack/fixture \
    && printf 'not compiler data' > /fixture/.next/cache/fetch-cache/private \
    && printf 'fixture built' > /compiler-fixture
FROM scratch
COPY --from=compiler-fixture /compiler-fixture /compiler-fixture
COPY --chmod=0555 fixture /fixture
USER 65532:65532
ENTRYPOINT ["/fixture"]
