# Build stage
FROM node:18-alpine AS build

WORKDIR /app

# Copy package files
COPY package*.json ./

# Install dependencies
RUN npm ci --legacy-peer-deps

# Copy source code (cache bust)
ARG CACHEBUST=1
COPY . .

# Build argument for API URL (set during Railway build)
ARG REACT_APP_API_URL
ENV REACT_APP_API_URL=$REACT_APP_API_URL
RUN if [ -z "$REACT_APP_API_URL" ]; then echo "REACT_APP_API_URL is required at build time"; exit 1; fi

# Build the app
RUN npm run build

# Production stage
FROM node:18-alpine

WORKDIR /app

# Install serve for static file serving
RUN npm install -g serve

# Copy built files from build stage
COPY --from=build /app/build ./build

# Expose port
EXPOSE 3000

# Start server - Railway provides PORT env var
CMD serve -s build -l ${PORT:-3000}
