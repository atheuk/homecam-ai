FROM node:22-alpine
WORKDIR /app
# Next.js inlines NEXT_PUBLIC_* into the client bundle at build time; a runtime
# env var cannot change it. Build with --build-arg NEXT_PUBLIC_API_URL=<API base URL>.
ARG NEXT_PUBLIC_API_URL
RUN test -n "$NEXT_PUBLIC_API_URL" || (echo "ERROR: build with --build-arg NEXT_PUBLIC_API_URL=<API base URL> (inlined into the browser bundle at build time)" >&2; exit 1)
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build
CMD ["npm","start"]
