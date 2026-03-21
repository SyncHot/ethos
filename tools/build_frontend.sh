#!/bin/bash
set -e

PROJECT_ROOT="/opt/ethos"
SRC_DIR="$PROJECT_ROOT/frontend"
DIST_DIR="$PROJECT_ROOT/frontend_dist"

# Ensure tools are available
if ! command -v terser &> /dev/null; then
    echo "Installing terser..."
    if command -v npm &> /dev/null; then
        sudo npm install -g terser
    else
        echo "Error: npm not found, cannot install terser."
        exit 1
    fi
fi

if ! command -v cleancss &> /dev/null; then
    echo "Installing clean-css-cli..."
    if command -v npm &> /dev/null; then
        sudo npm install -g clean-css-cli
    else
        echo "Error: npm not found, cannot install clean-css-cli."
        exit 1
    fi
fi

echo "Cleaning previous build..."
sudo rm -rf "$DIST_DIR"
sudo mkdir -p "$DIST_DIR"

echo "Copying assets..."
# Copy everything first
sudo cp -r "$SRC_DIR/"* "$DIST_DIR/"

# Minify JS
echo "Minifying JS..."
find "$DIST_DIR/js" -name "*.js" -type f | while read -r file; do
    echo "Minifying $file"
    sudo terser "$file" --compress --mangle -o "$file"
done

# Minify CSS
echo "Minifying CSS..."
find "$DIST_DIR/css" -name "*.css" -type f | while read -r file; do
    echo "Minifying $file"
    sudo cleancss -o "$file" "$file"
done

echo "Build complete. Assets are in $DIST_DIR"
