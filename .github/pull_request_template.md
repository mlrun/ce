### 📝 Description
<!-- A short summary of what this PR does. -->
<!-- Include any relevant context or background information. -->

---

### 🛠️ Changes Made
<!-- - Key changes (e.g., added feature X, refactored Y, fixed Z) -->

---

### ✅ Checklist
- [ ] I have tested the changes in this PR
- [ ] I confirmed whether my changes require a change in documentation and if so, I created another PR in MLRun for the relevant documentation.
- [ ] I confirmed whether my changes require a changes in QA tests, for example: credentials changes, resources naming change and if so, I updated the relevant Jira ticket for QA.
- [ ] I increased the Chart version in `charts/mlrun-ce/Chart.yaml`.
- [ ] I confirmed that the installation works both on a local Docker Desktop environment and on a real cluster when using the required [prerequisites](https://docs.mlrun.org/en/stable/install-mlrun-ce/kubernetes-install.html#prerequisites).
  - [ ] If installation issues were found, I updated the relevant Jira ticket with the issue and steps to reproduce, or updated the prerequisites documentation if the issue is related to missing or outdated prerequisites.   
- [ ] If needed, update https://github.com/mlrun/ce/blob/development/charts/mlrun-ce/README.md with the relevant installation instructions and version Matrix.
- [ ] If needed, update the following values files for multi namespace support:
  - [ ] [Admin values](https://github.com/mlrun/ce/blob/development/charts/mlrun-ce/admin_installation_values.yaml)
  - [ ] [User values Node Port](https://github.com/mlrun/ce/blob/development/charts/mlrun-ce/non_admin_installation_values.yaml)
  - [ ] [User values ClusterIP](https://github.com/mlrun/ce/blob/development/charts/mlrun-ce/non_admin_cluster_ip_installation_values.yaml)

---

### 🧪 Testing
<!-- - How it was tested (unit tests, manual, integration) -->  
<!-- - Any special cases covered. -->  

---

### 🔗 References
- Ticket link:
- External links:
- Design docs links (Optional):
---

### 🚨 Breaking Changes?

- [ ] Yes (explain below)
- [ ] No

<!-- If yes, describe what needs to be changed downstream: -->

---

### 🔍️ Additional Notes
<!-- Anything else reviewers should know (follow-up tasks, known issues, affected areas etc.). -->
<!-- ### 📸 Screenshots / Logs -->