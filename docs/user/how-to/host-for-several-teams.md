# Host Grimoire for several teams

This guide shows how to serve several teams from one Grimoire, so that each team sees its own repositories and nobody else's noise. It uses [spaces](../reference/configuration.md#spaces) and assumes Grimoire is already [deployed](deploy.md).

!!! warning "Spaces filter the view; they are not a security boundary"

    Every team shares one GitHub token, one git identity, one workspace and one database. Anyone who can open Grimoire can select any space, and anyone who can edit the configuration or a check or action script is effectively an administrator of the whole instance. Do not use spaces to keep teams apart; use them to keep the screens short.

## Give each team a space file

1. Create a `spaces.d/` directory next to your `config.yaml`. The paths in `space_files` are relative to the directory of the main config file, so in the Docker setup of [Deploy Grimoire](deploy.md#docker-compose) it lives in the mounted `config/` directory.

2. Add one file per team, with the keys of one [`spaces` entry](../reference/configuration.md#spaces):

    ```yaml
    # config/spaces.d/payments.yaml
    id: payments
    name: "Payments"
    repositories:
      - team: "org-name/payments"
      - repo: "org-name/shared-lib"
        workflows:
          include: ["CI"]
    ```

3. Point the main config at the directory:

    ```yaml
    # config/config.yaml
    space_files: ["spaces.d/*.yaml"]
    ```

4. Restart Grimoire. The navigation bar now shows a space selector, and the dashboard, backlog, checks, actions and PRs pages follow it.

The `id` must be unique across all files and any inline `spaces`; a duplicate stops the start-up with a message naming both places. A repository can appear in several spaces; it is fetched once.

Repositories that stay in the top-level `repositories` and in no space appear under **Unassigned**. To show nothing there, move them into a space.

## Let each team own its file

Because each space is a file, you can use your Git host's ownership rules to let a team change only its own space. With a `CODEOWNERS` file on GitHub:

```
/config/spaces.d/payments.yaml  @org-name/payments
/config/spaces.d/platform.yaml  @org-name/platform
/config/config.yaml             @org-name/grimoire-admins
```

Keep `config.yaml`, `data/` (checks and actions) and `mise.toml` owned by the people who run the instance: a space file can only add repositories to the view, but a check or action script runs with the instance's token and privileges.

## Scope checks and actions to a space

A check or action without `spaces` is listed under every space. To list one only under some spaces, add the ids:

```yaml
# data/checks/payments-lock-fresh.yaml
name: Payments lock is fresh
spaces: [payments]
# ...
```

This only changes where the check is **listed**. Which repositories it runs on is still decided by `targets`. An unknown id stops the start-up and names the check. See the [checks](../reference/checks.md) and [actions](../reference/actions.md) references.

## Put Grimoire behind your login

Grimoire has no authentication. For a shared instance, put it behind a VPN or an identity-aware proxy, so that only your colleagues can reach it.

- Make the proxy forward the original `Host` header. Grimoire rejects state-changing requests whose `Origin` names another host; see [Cross-site requests](../reference/api.md#cross-site-requests).
- Let the proxy skip authentication for `/health` and `/metrics`, so that container probes and Prometheus keep working. If the instance is served [under a path prefix](deploy.md#serving-under-a-path-prefix-ingress), the paths carry the prefix (for example `/grimoire/metrics`).
- Treat the instance as one trust domain: everyone who can sign in can see every space and trigger every check and action.

## Choose which spaces to show

Open the **Spaces** button next to the logo in the navigation bar. The button reads **Spaces** while everything is shown, the space's name when exactly one is selected, and **Spaces (2)**, **Spaces (3)** and so on for several. Hover it to see the selected names.

- Tick or untick a checkbox to combine spaces. The selection applies when you close the dropdown, by clicking outside it or pressing `Esc`.
- Click a space's name to show only that space. This applies at once.
- Select **All** or **None** to show everything again. Selecting every entry in the list is the same.
- Type in the search box to narrow a long list of spaces.

To send a colleague a filtered view, give them a link with the space ids in `?spaces=`, for example `https://grimoire.example.com/?spaces=payments,platform`. Opening it applies the selection and saves it in their browser. Use `?spaces=` with no value to show everything. Unknown ids are ignored, and if you serve Grimoire under a path prefix, the link includes the prefix.

The selection is stored per browser for a year. It is not a setting of the instance, so one person's choice does not change what others see.

## Check the result

1. Open Grimoire and click a space's name in the selector to show only that space. The dashboard, backlog and PRs pages now cover only that space's repositories, and the dashboard counts only those. The Checks and Actions pages list the checks and actions that have no `spaces` or that name this space, and show results only for that space's repositories.
2. Select two spaces. A repository that belongs to both appears once.
3. Reload the page, or close and reopen the browser: the selection is kept in the browser for a year.
4. Call `GET /api/spaces` to see the spaces and their repository counts, and `GET /api/repos/?spaces=payments` to filter the API. The API ignores the selection saved in the browser; see [Filtering by space](../reference/api.md#filtering-by-space).

To report per space in Prometheus, join on the `repo` label with `grimoire_repository_space_info`. The [metrics reference on GitHub](https://github.com/lucabello/grimoire/blob/main/METRICS.md#spaces) has the query.
