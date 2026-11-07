FROM alpine:3.24@sha256:3f4d6b32e26703c6082839234f43769d5046b3ac7c910033911d1e71d8cd630a AS compiler-fixture
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
