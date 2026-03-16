This is computing, like you never believed possible.



Multi-Solve Capability:
  - The task display pane is infinitely scalable
  - When concurrently solving a task using different approaches,
    the task display shrinks to show all concurrently running attempts
  - Taken to the extreme, the display is a full grid of potentially 100s or 1000s of agents running,
    and the UI would dynamically morph to a higher layer view

Adaptogenic Display:
  - A UI surface which dynamically adapts for multi-tasking, and
    can zoom in and out as needed into many different session states


Generative UI
  Z-axis (time) is just as important as 2D layout
  


Ideally you can reward compute with vouch points
The more compute you run, you can allocate that value to
endorsing things?



Generate the homepage

  - Three choices: Intro, Login, Signup

Intro - How to Present the Introduction
  o Avatar, Voice, Screen? (User chooses)
  o Depth: Summary, Details, Technical

"Hi. I guess we're meeting for the first time."
   Say something, gauge the reaction/response -> Store It
   Data feeds refinement / replacement cycle

Ability to manipulate and layout the screen with text
using color and font and spacing appropriately

And then to be able to build basic scaffolding / wireframe diagrams overlaid
And then to be able to display any web control in any place on the screen

New Identity Creation:
  - Identify available storage
  - 

email verify agent: it knows how to send an email with a unique code and verify it was read back



BlindHash
  $0.01 per call

You can also submit a signed statement (vouch) and it will unlock an AppID which when called against itself provides a key.

Keys are enrolled by providing the voucher, which is stored and saved along with a random AppID
Later you can lookup the AppID by providing the voucher with an updated timestamp

So the voucher itself has to be;
  1) The assertion ("Showed control of xxx@yyyy.zz")
  2) A proof of no-earlier-than timestamp ("Foundation Timestamp Agent")
  3) The signer ("Foundation Email Verification Agent")
  4) The signature

So you enroll with "Showed control of jeremy@auto.network" @ (last few minutes)
And you discover the key at that location using BlindHash
Optionally, you enable a notification if the AppID is ever accessed again
And then you encrypt your key and store it

This would allow BlindHash to unlock your key though, which is definitely unacceptable...
You have to blind it with a salt, which is what the user has to keep.
And then the response also has to be HMAC'd with the salt


auto.network

  - blocks
  - comms

blocks:
  - prompts and answers (chat history)
  - output file
    o fully versioned
    o revision graph and commit history
    o differences can be specifically flagged and accounted for
    o consistency scoring of some kind
  - essentially a 4th dimension of refinement and context/state accumulation
  - entirely based on given API keys, costs nothing above your own access to the given inference
  - auto-navigation of problem tree space
    o 
