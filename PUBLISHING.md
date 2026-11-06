# Publishing this fork on GitHub

A step-by-step guide for putting this version of TDSR on GitHub, telling the original
author about it, and later sending them smaller pull requests. Every step is done in
Terminal. Commands are on their own lines so you can copy them.

Your GitHub username, jonathan-s01, is already filled in throughout.

## Words used here

- Fork: your own copy of someone else's project on GitHub. You can change it freely.
- Upstream: the original project, tspivey/tdsr. Its main branch is called master.
- Branch: a named line of changes inside a project. Each pull request needs its own.
- Pull request (PR): a request asking the original author to take one branch of
  changes into their project.

## Part 1: one-time setup

1. You already have a GitHub account (jonathan-s01).
2. Install the GitHub command line tool. It handles signing in, forking and releases
   without using the website.

   ```
   brew install gh
   ```

3. Sign in. Choose GitHub.com, HTTPS, and "Login with a web browser". It shows a
   one-time code, then opens the browser; paste the code there.

   ```
   gh auth login
   ```

4. Tell git your name and email if you haven't before. Use the private address
   GitHub offers under Settings, Emails if you don't want your email public.

   ```
   git config --global user.name "Your Name"
   git config --global user.email "you@example.com"
   ```

## Part 2: create the fork and publish this version

1. Go to the folder you want the project in, for example your home folder.

   ```
   cd ~
   ```

2. Fork the original and download your fork. This creates a folder called tdsr
   with two remotes: origin (your fork) and upstream (the original). If a folder
   called tdsr already exists here, go somewhere else first, for example
   mkdir ~/github and cd ~/github.

   ```
   gh repo fork tspivey/tdsr --clone=true
   cd tdsr
   ```

3. Copy this version over the downloaded files. Change the first path to wherever
   you unzipped tdsr-v0.7.13. The trailing slashes matter.

   ```
   rsync -a --exclude .git ~/Downloads/tdsr-v0.7.13/ ./
   ```

4. Check nothing private is about to be published. This lists the files git will
   add. tdsr.cfg, tdsr.key, any .key file and tdsr.log must not appear; the
   .gitignore file already excludes them, so they normally won't.

   ```
   git status --short
   ```

5. Run the tests. The last line should say all tests passed.

   ```
   python3 test_tdsr.py | tail -1
   ```

6. Save and upload.

   ```
   git add -A
   git commit -m "TDSR fork version 0.7.13"
   git push
   ```

7. Make a release, so people get a fixed version and can read what changed.

   ```
   gh release create v0.7.13 --title "TDSR fork 0.7.13" --notes-file CHANGES.md
   ```

8. Check that installing from your fork works.

   ```
   uv tool install --force git+https://github.com/jonathan-s01/tdsr.git
   ```

Your fork is at https://github.com/jonathan-s01/tdsr. For later versions, repeat
steps 3, 5 and 6, then step 7 with the new version number.

## Part 3: tell the original author

Open an issue on the original project. Pull requests are usually welcome more when
the author has agreed to them first, especially for a fork this size.

```
gh issue create --repo tspivey/tdsr --title "A fork with scrollback, say all and other additions" --body-file ~/ISSUE.md
```

Before running that, create ISSUE.md in your home folder with something like the
text below, edited to sound like you.

```
Hi Tyler. Thank you for TDSR. I use it daily with a screen reader and have built a
fork with a number of additions, published at
https://github.com/jonathan-s01/tdsr. The list is in CHANGES.md there.

The fork has grown large, so I don't expect you to take it whole. If any parts are
useful, I'd be glad to send them as separate small pull requests. Ones that might
be easiest to take:

- a fix for multi-line selections copying the wrong region
- arrow keys waiting for the program to finish redrawing before speaking
- scrollback review
- say all

Let me know which, if any, you'd like.
```

## Part 4: sending a smaller pull request

Each pull request is one branch that starts from the original's code and contains
only one feature. Our tdsr.py has changed a lot, so a feature can't just be copied
across. When the author says which parts they want, ask Copilot for a patch file
for that feature and upload the original's tdsr.py from the upstream folder
(git fetch upstream, then git show upstream/master:tdsr/tdsr.py > upstream-tdsr.py).

Then, for each feature (scrollback is used as the example name):

1. Get the original's latest code and start a branch from it.

   ```
   git fetch upstream
   git switch -c scrollback upstream/master
   ```

2. Apply the patch file. If git reports that it does not apply, stop and ask for
   an updated patch.

   ```
   git apply ~/Downloads/scrollback.patch
   ```

3. Save and upload the branch.

   ```
   git add -A
   git commit -m "Add scrollback review"
   git push -u origin scrollback
   ```

4. Open the pull request against the original's master branch. It opens an editor
   for the description; or add --title and --body to give them directly.

   ```
   gh pr create --repo tspivey/tdsr --base master --head jonathan-s01:scrollback
   ```

5. Go back to your own version when finished.

   ```
   git switch master
   ```

If the author asks for changes, switch to that branch, make them, commit and push
again; the pull request updates by itself.

## License

TDSR is GPL version 3. You may publish a modified version as long as COPYING.txt
stays, the source is available (GitHub covers that), and it says it was modified.
The readme's opening paragraph does that.
