JobSync 1.2.2.10 - Administrator controls

Admin Panel:
- Rename members and manage their roles (admin/moderator/member).
- Kick/block members and unblock them later.
- Remove members from the local JobSync workspace roster.
- Add/register members to the local workspace roster.
- Update the current admin account name and login email.

Settings:
- Admins can add/remove custom navigation sections.
- Workspace-wide job provider/source, ATS, Apify, Supabase presence and Google OAuth settings are admin-only.
- Personal LinkedIn profile/notification settings remain available to normal users.

Important architecture note:
- JobSync's local authentication is one account per installation. Member records are a local workspace roster. A remote member's own laptop/account is not deleted by the local "Remove member" action; the action removes/blocklists the member from this installation's roster.
