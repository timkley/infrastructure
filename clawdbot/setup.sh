#!/bin/bash
set -e

echo "=== Clawdbot Setup Script ==="
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check prerequisites
echo "Checking prerequisites..."

# Check Docker
if ! command -v docker &> /dev/null; then
    echo -e "${RED}Error: Docker is not installed${NC}"
    exit 1
fi

if ! command -v docker compose &> /dev/null; then
    echo -e "${RED}Error: Docker Compose is not installed${NC}"
    exit 1
fi

# Check if clawdbot repo exists
REPO_PATH="/opt/clawdbot"
if [ ! -d "$REPO_PATH" ]; then
    echo -e "${RED}Error: Clawdbot repository not found at $REPO_PATH${NC}"
    echo ""
    echo "Please clone it first:"
    echo "  git clone https://github.com/clawdbot/clawdbot.git /opt/clawdbot"
    exit 1
fi

echo -e "${GREEN}✓${NC} Prerequisites OK"
echo ""

# Create .env if it doesn't exist
if [ ! -f .env ]; then
    echo "Creating .env from .env.example..."
    cp .env.example .env
    echo -e "${GREEN}✓${NC} Created .env file"
else
    echo -e "${YELLOW}Note: .env already exists, skipping creation${NC}"
fi
echo ""

# Build the gateway image
echo "Building clawdbot Docker image..."
docker compose build clawdbot-gateway
echo -e "${GREEN}✓${NC} Image built successfully"
echo ""

# Run onboard wizard
echo "Running onboard wizard..."
echo -e "${YELLOW}Please follow the prompts to configure your gateway${NC}"
echo ""
echo "After the wizard completes, you'll be asked to paste the generated token."
echo ""

# Run onboard interactively
docker compose run --rm clawdbot-cli onboard --no-install-daemon

# Prompt user to paste the token
echo ""
echo "The onboard wizard should have displayed a gateway token."
echo "Please paste the token here (it will be saved to .env):"
read -r TOKEN

if [ -z "$TOKEN" ]; then
    echo ""
    echo -e "${RED}Error: No token provided${NC}"
    echo "Please add the token to .env manually:"
    echo ""
    echo "  CLAWDBOT_GATEWAY_TOKEN=your-token-here"
    echo ""
    echo "Then start the gateway with:"
    echo "  docker compose up -d clawdbot-gateway"
    exit 1
fi

# Save token to .env
echo ""
echo "Saving gateway token to .env..."

# Check if token already exists in .env
if grep -q "^CLAWDBOT_GATEWAY_TOKEN=" .env; then
    # Update existing token
    sed -i.bak "s/^CLAWDBOT_GATEWAY_TOKEN=.*/CLAWDBOT_GATEWAY_TOKEN=$TOKEN/" .env
    rm .env.bak 2>/dev/null || true
else
    # Append token
    echo "CLAWDBOT_GATEWAY_TOKEN=$TOKEN" >> .env
fi

echo -e "${GREEN}✓${NC} Token saved to .env"
echo ""

# Start the gateway
echo "Starting clawdbot gateway..."
docker compose up -d clawdbot-gateway
echo -e "${GREEN}✓${NC} Gateway started"
echo ""

# Show status
echo "Checking gateway status..."
sleep 2
docker compose ps clawdbot-gateway
echo ""

echo -e "${GREEN}=== Setup Complete! ===${NC}"
echo ""
echo "Next steps:"
echo ""
echo "1. Access Control UI:"
echo "   https://clawdbot.timkley.dev"
echo "   (Use the token from .env to authenticate)"
echo ""
echo "2. Set up Claude authentication:"
echo "   docker compose run --rm clawdbot-cli bash"
echo "   claude setup-token"
echo "   exit"
echo ""
echo "3. Link WhatsApp:"
echo "   docker compose run --rm clawdbot-cli channels login"
echo ""
echo "4. View logs:"
echo "   docker compose logs -f clawdbot-gateway"
echo ""
echo "For more information, see SETUP.md"
