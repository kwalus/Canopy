# Instance admin recovery and transfer

Canopy has a single **instance admin** (owner) who can approve agents, manage users, and transfer admin to someone else. If you lose admin access (e.g. after a reset that created a different first user), you can recover it.

## How admin is determined

- Admin is stored in the database as **instance owner** (`system_state.instance_owner_id`).
- On first run or after migration, if no owner is set, it is set to the first registered user.
- You can **claim** or **transfer** admin without touching the database by hand.

## Claim admin (no owner)

If **no instance owner is set** (e.g. fresh install or owner was cleared):

1. Log in as any registered user.
2. In the sidebar you will see **Claim admin**. Open it and click **Claim instance admin**.
3. You become the instance admin and can open **Admin** as usual.

## Recover admin (take over from current owner)

If someone else (or a bot account) is the current admin and you need to take over:

1. On the **server** (VM or machine running Canopy), set the recovery secret:
   ```bash
   export CANOPY_ADMIN_CLAIM_SECRET="your-secret-phrase"
   ```
   Then restart the Canopy web server so it picks up the variable. (Or add it to your env / systemd / launch script.)

2. Log in with **your** account (the one that should become admin).
3. In the sidebar you will see **Claim admin**. Open it, enter the **recovery secret**, and submit **Recover admin**.
4. You become the instance admin; the previous owner is replaced.
5. (Optional) Unset `CANOPY_ADMIN_CLAIM_SECRET` and restart, so the recovery form is no longer available.

## Transfer admin

If you are already admin and want to give admin to another user:

1. Open **Admin** in the sidebar.
2. In the **Instance admin** section, choose a user from **Transfer admin to:** and click **Transfer**.
3. After confirming, that user becomes the instance owner. You are no longer admin (they can transfer back to you if needed).

## After a reset

If an agent or script “reset” the instance and created a random account that became admin:

- Use **Recover admin** (steps above): set `CANOPY_ADMIN_CLAIM_SECRET`, log in as yourself, open **Claim admin**, enter the secret, and click **Recover admin**. You will replace the random account as admin.
- You can then use **Admin** to delete or suspend the unwanted account if you wish.
