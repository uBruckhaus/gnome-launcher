#!/usr/bin/env bash
# Automated Publisher for Simple Launcher for Fools
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"

echo "=== Simple Launcher for Fools: Marketplace Publisher ==="

# Check gh authentication
if ! gh auth status >/dev/null 2>&1; then
    echo "GitHub CLI is not authenticated yet."
    echo "Starting authentication..."
    gh auth login -h github.com -p https --web
fi

GH_USER="$(gh api user --jq .login 2>/dev/null || true)"
if [[ -z "$GH_USER" ]]; then
    echo "Error: Could not retrieve GitHub username."
    exit 1
fi

echo "Authenticated as: $GH_USER"
REPO_NAME="omarchy-simple-launcher-for-fools"
REPO_URL="https://github.com/$GH_USER/$REPO_NAME"

# Check if remote already exists
if ! git remote get-url origin >/dev/null 2>&1; then
    echo "Creating public repository: $REPO_NAME..."
    gh repo create "$REPO_NAME" --public --source=. --remote=origin --push || {
        echo "Adding remote and pushing..."
        git remote add origin "$REPO_URL.git" || true
        git push -u origin main
    }
else
    echo "Pushing latest commits to origin..."
    git push -u origin main
fi

echo "Repository published to: $REPO_URL"

# Generate issue body for marketplace submission
SUBMISSION_FILE="$(mktemp /tmp/marketplace-submission-XXXXXX.md)"
cat << EOF > "$SUBMISSION_FILE"
### Repository URL

$REPO_URL

### Category

Desktop

### Tags

Launcher, Bar, Quickshell

### Maintainer notes

Fast, theme-aware application launcher popup and native Quickshell bar widget for Omarchy. Powered by GTK4 Layer Shell and Python standard library/GIO. Cleanly validated via 'omarchy plugin validate'. Includes preview.png.

### Submission checklist

- [x] The repository is public and contains installation and removal instructions.
- [x] I have documented the plugin license and any external dependencies.
- [x] I confirm that I own or have permission to submit this plugin and its preview assets.
- [x] The plugin does not overwrite user configuration without explicit consent.
- [x] I understand that approval is for listing and is not a security review.
EOF

echo "Submitting to omacom/omarchy-plugin-marketplace..."
ISSUE_URL="$(gh issue create \
    --repo omacom/omarchy-plugin-marketplace \
    --title "[Plugin]: Simple Launcher for Fools" \
    --body-file "$SUBMISSION_FILE")"

rm -f "$SUBMISSION_FILE"

echo ""
echo "🎉 SUCCESS! Plugin submitted to the Omarchy Marketplace!"
echo "Issue URL: $ISSUE_URL"
echo "Repository: $REPO_URL"
