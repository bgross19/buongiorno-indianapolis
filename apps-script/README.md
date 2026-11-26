# Buongiorno, Indianapolis! — Cloud Dispatcher Setup

GitHub Actions `schedule` triggers can be delayed by 10–12 hours due to platform scheduler backlogs, whereas `workflow_dispatch` runs start immediately. This Google Apps Script runs in the cloud every 15 minutes (24/7, even when your Mac is asleep), checks Google Drive for any unpublished `YYYY-MM-DD.json` episode script (or updated script metadata), and dispatches `.github/workflows/publish.yml` via the GitHub API.

## One-Time Setup (as `bgross19@gmail.com`)

1. **Create a fine-grained GitHub Personal Access Token:**
   - Open [https://github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new).
   - Under **Repository access**, select **Only select repositories** → `bgross19/buongiorno-indianapolis`.
   - Under **Repository permissions**, set:
     - **Actions:** `Read and write`
     - **Contents:** `Read-only`
     - *(Metadata is selected automatically.)*
   - Generate and copy the token.

2. **Create the Apps Script project:**
   - Open [https://script.google.com](https://script.google.com) while signed in as `bgross19@gmail.com`.
   - Click **New project** and rename it to **`Buongiorno Indianapolis Dispatcher`**.

3. **Paste the dispatcher code:**
   - Replace the contents of `Code.gs` with [`apps-script/dispatch.gs`](./dispatch.gs) and save (`Cmd+S`).

4. **Store the GitHub token in Script Properties:**
   - Open **Project Settings** (gear icon) → **Script Properties** → **Add script property**.
   - **Property:** `GITHUB_TOKEN`
   - **Value:** your fine-grained PAT from Step 1
   - Click **Save script properties**.

5. **Install the 15-minute cloud trigger:**
   - Return to the **Editor**, select **`setupTrigger`** from the function dropdown, and click **Run**.
   - Approve the Google authorization prompt once (Google Drive read + external HTTPS request to GitHub).
   - Verify the execution log confirms the trigger was installed and the initial Drive/GitHub check succeeded.

6. **Optional end-to-end test:**
   - Select **`testDispatchNow`** from the function dropdown and click **Run** to confirm a `workflow_dispatch` run starts immediately under the **Actions** tab on GitHub.
