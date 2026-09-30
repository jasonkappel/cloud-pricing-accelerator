#!/usr/bin/env bash
# Installs or updates the harvester on the VM. Run as root through Azure Run Command by
# scripts/deploy-harvester.ps1; safe to rerun. Arguments: account URL, identity client ID,
# Azure region, AWS region, approval key ID.
set -euo pipefail

if [ "$#" -ne 5 ]; then
    echo "usage: install.sh <price-book-blob-url> <identity-client-id> <azure-region> <aws-region> <approval-key-id>" >&2
    exit 2
fi
account_url=$1
client_id=$2
azure_region=$3
aws_region=$4
approval_key_id=$5

[[ "$account_url" =~ ^https://[a-z0-9]{3,24}\.blob\.core\.(windows\.net|usgovcloudapi\.net|chinacloudapi\.cn)/?$ ]] \
    || { echo "Invalid price book blob URL." >&2; exit 2; }
[[ "$client_id" =~ ^[0-9a-fA-F-]{36}$ ]] || { echo "Invalid identity client ID." >&2; exit 2; }
[[ "$azure_region" =~ ^[a-z0-9]{2,40}$ ]] || { echo "Invalid Azure region." >&2; exit 2; }
[[ "$aws_region" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]$ ]] || { echo "Invalid AWS region." >&2; exit 2; }
# A versioned key ID, so a rotated key never silently verifies old approvals.
[[ "$approval_key_id" =~ ^https://[a-z0-9-]{3,24}\.vault\.(azure\.net|usgovcloudapi\.net|azure\.cn)/keys/[A-Za-z0-9-]{1,127}/[0-9a-f]{32}$ ]] \
    || { echo "Invalid approval key ID." >&2; exit 2; }

package=$(cd "$(dirname "$0")/.." && pwd)
root=/opt/pricing-harvester
release="$root/releases/$(date -u +%Y%m%dT%H%M%SZ)"

echo "== first boot"
cloud-init status --wait >/dev/null || true

echo "== data disk"
/usr/local/sbin/check-harvester-disk

echo "== packages (HTTPS mirror; the network allows only HTTPS out)"
if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then
    sed -i -E 's#^URIs: http://[^ ]+#URIs: https://archive.ubuntu.com/ubuntu/#' /etc/apt/sources.list.d/ubuntu.sources
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3.12-venv

echo "== service account"
id -u pricing-harvester >/dev/null 2>&1 \
    || useradd --system --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin pricing-harvester
install -d -o pricing-harvester -g pricing-harvester -m 0700 /var/lib/pricing-harvester/runs

echo "== code"
install -d -m 0755 "$root" "$root/releases" "$root/bin" /etc/pricing-harvester
install -d -m 0755 "$release"
cp -r "$package/harvester" "$release/harvester"
chmod -R u=rwX,go=rX "$release"
ln -sfn "$release" "$root/app"
install -m 0755 "$package/vm/harvest-start" "$root/bin/harvest-start"
install -m 0755 "$package/vm/harvest-finish" "$root/bin/harvest-finish"

echo "== Python environment"
[ -x "$root/venv/bin/python" ] || python3.12 -m venv "$root/venv"
"$root/venv/bin/python" -m pip install --quiet --disable-pip-version-check --no-cache-dir \
    -r "$release/harvester/requirements.txt"

echo "== configuration"
umask 022
cat > /etc/pricing-harvester/harvest.env <<EOF
HARVESTER_ACCOUNT_URL=${account_url%/}
HARVESTER_CLIENT_ID=$client_id
HARVEST_AZURE_REGION=$azure_region
HARVEST_AWS_REGION=$aws_region
APPROVAL_KEY_ID=$approval_key_id
EOF
install -m 0644 "$package/vm/pricing-harvest.service" /etc/systemd/system/pricing-harvest.service
systemctl daemon-reload
systemctl enable pricing-harvest.service

echo "== smoke test"
cd "$root/app"
runuser -u pricing-harvester -- "$root/venv/bin/python" -m harvester --help >/dev/null
runuser -u pricing-harvester -- "$root/venv/bin/python" -c "import harvester.vm_signal, harvester.published_fetch, harvester.approval_keys, azure.keyvault.keys"

# Keep the three newest releases.
ls -1dt "$root"/releases/* | tail -n +4 | xargs -r rm -rf

echo "HARVESTER_INSTALL_OK $(basename "$release")"
