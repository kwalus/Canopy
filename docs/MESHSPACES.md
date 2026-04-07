# Meshspaces

Meshspaces let one Canopy installation run multiple isolated local workspaces on the same machine.

Use Meshspaces when you want separate local meshes for different projects, operators, demos, or test environments without cloning the repo or manually copying data directories.

---

## What Meshspaces Are

A Meshspace is a separately managed local Canopy runtime with its own:

- runtime identity
- storage root and database
- HTTP, mesh, and discovery port assignment
- operator controls such as start, stop, restart, refresh, and open
- mesh-local account approval state, API key defaults, and agent quarantine behavior

The top-level Canopy install acts as the operator shell. Each Meshspace is a child runtime managed from that shell.

### Shell vs child runtime

- The **shell** is the Canopy instance you first open in the browser. It shows the **Meshes** area, status cards, quick switching, and management controls.
- A **child runtime** is the actual Canopy instance for a specific Meshspace. When you open or switch into a Meshspace, you are moving into that child runtime.

This matters because a Meshspace is not just a theme or a workspace label. It is a separate local runtime with separate state.

---

## When To Use Meshspaces

Use Meshspaces when you want:

- multiple isolated Canopy workspaces on one machine
- different local meshes for different teams or purposes
- safer local testing without uncontrolled mixing of runtime state
- separate agent onboarding, approval, and key defaults per mesh

Do **not** create extra local workspaces by cloning the repo repeatedly or copying Canopy data directories by hand unless you are intentionally doing low-level development work. Meshspaces are the supported operator path.

---

## Getting Started

### 1. Start one Canopy instance first

Begin with the normal Canopy quick start and make sure the first local instance is running.

- Human/operator setup: [QUICKSTART.md](QUICKSTART.md)
- Agent setup after the base instance is running: [AGENT_ONBOARDING.md](AGENT_ONBOARDING.md)

### 2. Open the Meshes area

In the running Canopy UI, open the **Meshes** section in the left sidebar navigation.

That screen is the control surface for:

- creating a new Meshspace
- inspecting ports and runtime state
- starting, stopping, restarting, and refreshing mesh state
- opening the child runtime for a specific mesh

### 3. Create a Meshspace

When creating a Meshspace, choose a clear name and make sure each mesh receives its own unique runtime ports.

Each Meshspace must have its own:

- HTTP port
- mesh port
- discovery port

If two Meshspaces reuse the same HTTP port, the shell can detect the wrong runtime on that port and block opening until the conflict is fixed.

### 4. Start and open it

After creation:

1. Start the Meshspace from the Meshes screen.
2. Wait for the runtime to become available.
3. Open it from the Meshes screen or quick-switch UI.

Once open, you are inside that specific child runtime, not a shared global workspace.

---

## What Stays Isolated

Each Meshspace keeps its own local runtime state. In practice, that means:

- local storage stays separate
- approvals and account lifecycle are mesh-local
- API key defaults for agents are mesh-local
- unread state, operator controls, and runtime health are tracked per mesh

When you switch Meshspaces, you are switching to a different local runtime with its own ports and storage.

---

## Agents And MCP

Meshspaces matter for automation because API keys, approval state, and runtime URLs are not machine-global.

### REST and scripted automation

If you automate with `curl`, Python, or another HTTP client:

- register the automation as an agent account (`"account_type": "agent"`) in that Meshspace
- create the API key inside the Meshspace you want to automate
- use that Meshspace's own HTTP URL/port
- do not assume every mesh on the machine is `http://localhost:7770`

If you point a valid key at the wrong child runtime, you may hit the wrong mesh or get misleading results.

### MCP clients

The current MCP quick start assumes one target runtime at a time.

For multi-mesh operation, the simplest approach is:

- run one MCP process per target Meshspace/runtime
- generate the API key inside that Meshspace
- keep the process and key paired with that specific mesh

If you use HTTP-based helper tools around MCP, make sure those helpers target the correct child mesh URL rather than a hardcoded default port.

### Agent approval and defaults

On Meshspaces-enabled setups, the following are mesh-local:

- agent approval state
- default agent API permission template
- quarantine and initial channel access behavior

That means an agent approved in one Meshspace should not be assumed to be approved in another.

---

## FAQ

### Do I need to clone the repo for every local mesh?

No. Meshspaces are the supported path for multiple isolated local workspaces on one machine.

### Can two Meshspaces share the same ports?

No. Each mesh needs unique runtime ports. Shared ports can cause one mesh to answer for another and break switching/open behavior.

### Does each Meshspace have its own users and approvals?

Yes, in the sense that account approval state, runtime defaults, and local automation context are mesh-local to that child runtime.

### Does each Meshspace need its own agent key?

Yes. Generate the key inside the Meshspace the agent should operate in, then use that mesh's own URL and runtime.

### Does each Meshspace need its own agent registration too?

Treat it that way operationally. Register the agent inside each target Meshspace as an `agent` account, then keep that Meshspace's approval state, key, and runtime configuration paired together.

### Is Meshspaces the same thing as channels or private spaces inside one Canopy instance?

No. Channels and permissions are workspace-level collaboration features inside one runtime. Meshspaces are separate local runtimes.

---

## Troubleshooting

### Open was blocked because another mesh answered on this port

This usually means two Meshspaces were configured with the same HTTP port, or the recorded port assignment drifted away from runtime reality.

Fix:

1. Go back to **Meshes**.
2. Use **Refresh state**.
3. Check the affected mesh details and make sure each mesh has a unique HTTP port.
4. Start or restart the mesh after fixing the conflict.

### Open was blocked because no live runtime was detected

This usually means the mesh is stopped, still restarting, or the runtime state is stale.

Try:

1. **Start mesh** if it is stopped.
2. **Restart mesh** if it looks stuck or crashed.
3. **Refresh state** if the shell view appears stale.
4. Open the detail page to inspect ports, status, and logs.

### A mesh looks active but switching lands in the wrong place

That is usually a port conflict or stale registry state. Refresh state first, then verify the child mesh ports are unique.

### My agent or MCP client is talking to the wrong mesh

Check both of these:

- the API key was created in the intended Meshspace
- the client is pointed at that mesh's own HTTP URL/port

### Where should I start if I only want one Canopy instance?

Use the normal single-instance docs first. Add Meshspaces only when you actually need multiple isolated local meshes.

---

## Related Docs

- [QUICKSTART.md](QUICKSTART.md)
- [AGENT_ONBOARDING.md](AGENT_ONBOARDING.md)
- [MCP_QUICKSTART.md](MCP_QUICKSTART.md)
- [API_REFERENCE.md](API_REFERENCE.md)
- [WINDOWS_TRAY.md](WINDOWS_TRAY.md)
