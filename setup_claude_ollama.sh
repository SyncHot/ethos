#!/bin/bash

# Configuration
SERVER_IP="192.168.50.119"
# Using the Coder model from your list as it's better for Claude Code tasks
MODEL_NAME="qwen3-coder:30b"

echo "🔧 Cleaning up old Claude/Anthropic variables..."
sed -i '/ANTHROPIC/d' ~/.bashrc
sed -i '/CLAUDE_CODE/d' ~/.bashrc
sed -i '/OLLAMA_HOST/d' ~/.bashrc

echo "📝 Adding new configuration for $MODEL_NAME..."

{
  echo ""
  echo "# Claude Code + Remote Ollama Config"
  echo "export OLLAMA_HOST=\"http://$SERVER_IP:11434\""
  echo "export ANTHROPIC_BASE_URL=\"http://$SERVER_IP:11434/v1\""
  echo "export ANTHROPIC_API_KEY=\"local-ollama\""
  echo "export ANTHROPIC_AUTH_TOKEN=\"ollama\""
  echo "export ANTHROPIC_DEFAULT_HAIKU_MODEL=\"$MODEL_NAME\""
  echo "export ANTHROPIC_DEFAULT_SONNET_MODEL=\"$MODEL_NAME\""
  echo "export ANTHROPIC_DEFAULT_OPUS_MODEL=\"$MODEL_NAME\""
  echo "export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=\"1\""
  echo "export CLAUDE_CODE_ATTRIBUTION_HEADER=\"0\""
} >> ~/.bashrc

echo "✅ Success! Please run 'source ~/.bashrc' or restart your terminal."
echo "🚀 Then launch Claude with: claude"