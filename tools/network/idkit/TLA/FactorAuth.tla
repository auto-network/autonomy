------------------------------- MODULE FactorAuth -----------------------------
(***************************************************************************)
(* Formal model of the personal-identity FACTOR / AUTH state machine: the  *)
(* set of ways the personal-root master KEK can be armored (password,      *)
(* passkey, combined MFA), the transitions the factor-management UI and    *)
(* the re-arm backend expose, and the safety properties that every         *)
(* reachable configuration must keep.                                      *)
(*                                                                         *)
(* Plan of record = the UI.  The factor model encoded here is the          *)
(* operator-confirmed one recorded in                                      *)
(*   /workspace/output/factor-ui/UNDERSTANDING.md :                        *)
(*                                                                         *)
(*   - Two INDIVIDUAL factors each open the root ALONE: password, passkey. *)
(*     Valid: password-only, passkey-only, both-individual (OR).           *)
(*   - MFA = ONE combined password+PRF factor; enabling it CLEARS the      *)
(*     individual factors (no standalone opener survives beside it).       *)
(*   - Reach MFA two ways: found a fresh identity straight to combined, or  *)
(*     (existing identity) have one factor, add the other, combine —        *)
(*     EITHER order.                                                       *)
(*   - rootReachable: never delete the last opener.                        *)
(*                                                                         *)
(* Green configurations (all design switches at the CORRECT value) must    *)
(* check clean.  Each calibration flips one switch back to a broken design *)
(* and TLC must rediscover the corresponding violation on its own.  Probe  *)
(* configurations assert that a target state is REACHABLE by expecting a    *)
(* "never reach it" invariant to fail.  See MODEL.md for the abstraction   *)
(* ledger and README.md for the run/honesty rules.                        *)
(*                                                                         *)
(* Crypto is symbolic (idealized): a factor opens the KEK iff its holder    *)
(* supplies the factor's material; a statement verifies iff checked against *)
(* the ledger root public key.  The model proves the PROTOCOL / state       *)
(* machine sound; it does not prove the primitives, nor that the code       *)
(* conforms (a separate obligation named by the honesty rule).             *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS
    PASSKEYS,            \* set of passkey device slots, e.g. {"pk1","pk2"}
    \* ---- design switches: the value shown is the CORRECT (green) one ----
    ClearOnCombine,      \* TRUE  : enabling MFA clears the individual factors
                         \* FALSE : (calibration) individuals survive beside MFA
    GuardLastOpener,     \* TRUE  : removal refused if it drops the last opener
                         \* FALSE : (calibration) rootReachable not enforced
    TrustLedgerPub,      \* TRUE  : statements verified vs ledger root_pub
                         \* FALSE : (calibration) trust the payload's own signer
    ZeroSeedAfterUse,    \* TRUE  : plaintext secret zeroed at end of ceremony
                         \* FALSE : (calibration) plaintext secret persisted
    AllowUpgradeMFA      \* TRUE  : EnableMFA (upgrade path) available
                         \* FALSE : (probe) only the fresh-found path reaches MFA

ASSUME ClearOnCombine  \in BOOLEAN
ASSUME GuardLastOpener \in BOOLEAN
ASSUME TrustLedgerPub  \in BOOLEAN
ASSUME ZeroSeedAfterUse \in BOOLEAN
ASSUME AllowUpgradeMFA \in BOOLEAN

NoPk == "none"                  \* the empty value for the combined-factor slot
ASSUME NoPk \notin PASSKEYS

VARIABLES
    identity,        \* BOOLEAN: has a personal identity been founded yet
    hasPassword,     \* BOOLEAN: a STANDALONE password factor wraps the KEK
    role,            \* [PASSKEYS -> {"unenrolled","access","root"}]
                     \*   unenrolled: device not registered
                     \*   access    : enrolled; dashboard session key only,
                     \*               does NOT open the root
                     \*   root      : enrolled AND a standalone root factor
    combined,        \* NoPk, or the passkey bound into the combined MFA factor
    pubPublished,    \* [PASSKEYS -> BOOLEAN]: provisioning public key of the
                     \*   passkey published to the ledger at enrollment
    plainSecretLoc,  \* location of the plaintext root secret (seed / master KEK)
                     \*   "none" between ceremonies; a broken design may leak it
    provOK           \* BOOLEAN: every statement verified so far used the ledger
                     \*   root public key, never the payload's own signer

vars == <<identity, hasPassword, role, combined, pubPublished,
          plainSecretLoc, provOK>>

-------------------------------------------------------------------------------
(* Derived predicates. *)

RootPasskeyExists == \E pk \in PASSKEYS : role[pk] = "root"

\* A configuration can open the root iff a standalone password, a standalone
\* root passkey, or the combined (both-material) factor is present.
OpenerExists == hasPassword \/ RootPasskeyExists \/ combined # NoPk

OpenerExistsWithout(gone) ==
    \/ hasPassword
    \/ (\E pk \in PASSKEYS : pk # gone /\ role[pk] = "root")
    \/ (combined # NoPk /\ combined # gone)

\* Value the plaintext secret ends an armor-touching ceremony in.
SecretEnd == IF ZeroSeedAfterUse THEN "none" ELSE "disk"

\* Provenance of a statement check: correct design uses the ledger key.
ProvAfter == IF TrustLedgerPub THEN provOK ELSE FALSE

-------------------------------------------------------------------------------
(* Initial state: nothing founded. *)

Init ==
    /\ identity = FALSE
    /\ hasPassword = FALSE
    /\ role = [pk \in PASSKEYS |-> "unenrolled"]
    /\ combined = NoPk
    /\ pubPublished = [pk \in PASSKEYS |-> FALSE]
    /\ plainSecretLoc = "none"
    /\ provOK = TRUE

-------------------------------------------------------------------------------
(* Founding transitions: nothing -> a first valid factor configuration. *)

FoundFreshPassword ==
    /\ ~identity
    /\ identity' = TRUE
    /\ hasPassword' = TRUE
    /\ plainSecretLoc' = SecretEnd
    /\ UNCHANGED <<role, combined, pubPublished, provOK>>

FoundFreshPasskey ==
    \E pk \in PASSKEYS :
        /\ ~identity
        /\ identity' = TRUE
        /\ role' = [role EXCEPT ![pk] = "root"]
        /\ pubPublished' = [pubPublished EXCEPT ![pk] = TRUE]
        /\ plainSecretLoc' = SecretEnd
        /\ provOK' = ProvAfter
        /\ UNCHANGED <<hasPassword, combined>>

\* nothing -> password+PRF combined directly (fresh identity, no intermediate
\* single-factor state).  The property the operator asked to be proven safe.
FoundFreshCombined ==
    \E pk \in PASSKEYS :
        /\ ~identity
        /\ identity' = TRUE
        /\ combined' = pk
        /\ role' = [role EXCEPT ![pk] = "access"]
        /\ pubPublished' = [pubPublished EXCEPT ![pk] = TRUE]
        /\ plainSecretLoc' = SecretEnd
        /\ provOK' = ProvAfter
        /\ UNCHANGED <<hasPassword>>

-------------------------------------------------------------------------------
(* Passkey enrollment / promotion (the upgrade lane). *)

\* Enroll a device as ACCESS-only: derives a dashboard session key; publishes
\* the provisioning public key; verifies a root-signed enrollment statement.
\* Does NOT touch the root plaintext.
EnrollPasskeyAccess ==
    \E pk \in PASSKEYS :
        /\ identity
        /\ role[pk] = "unenrolled"
        /\ role' = [role EXCEPT ![pk] = "access"]
        /\ pubPublished' = [pubPublished EXCEPT ![pk] = TRUE]
        /\ provOK' = ProvAfter
        /\ UNCHANGED <<identity, hasPassword, combined, plainSecretLoc>>

\* Promote an access passkey to a standalone root factor (opens armor with an
\* existing opener, re-wraps the KEK to the passkey's provisioning key).
PromotePasskeyToRoot ==
    \E pk \in PASSKEYS :
        /\ identity
        /\ combined = NoPk
        /\ role[pk] = "access"
        /\ pubPublished[pk]
        /\ OpenerExists
        /\ role' = [role EXCEPT ![pk] = "root"]
        /\ plainSecretLoc' = SecretEnd
        /\ provOK' = ProvAfter
        /\ UNCHANGED <<identity, hasPassword, combined, pubPublished>>

-------------------------------------------------------------------------------
(* Password add / remove (the upgrade lane). *)

AddPassword ==
    /\ identity
    /\ combined = NoPk
    /\ ~hasPassword
    /\ OpenerExists
    /\ hasPassword' = TRUE
    /\ plainSecretLoc' = SecretEnd
    /\ UNCHANGED <<identity, role, combined, pubPublished, provOK>>

RemovePassword ==
    /\ identity
    /\ combined = NoPk
    /\ hasPassword
    /\ (GuardLastOpener => RootPasskeyExists)
    /\ hasPassword' = FALSE
    /\ plainSecretLoc' = SecretEnd
    /\ UNCHANGED <<identity, role, combined, pubPublished, provOK>>

-------------------------------------------------------------------------------
(* Demote / remove a passkey. *)

\* Root passkey -> access-only (drops its standalone root authority).
DemotePasskey ==
    \E pk \in PASSKEYS :
        /\ identity
        /\ combined = NoPk
        /\ role[pk] = "root"
        /\ (GuardLastOpener => OpenerExistsWithout(pk))
        /\ role' = [role EXCEPT ![pk] = "access"]
        /\ plainSecretLoc' = SecretEnd
        /\ UNCHANGED <<identity, hasPassword, combined, pubPublished, provOK>>

\* Remove a device entirely.  Refused while it is a root factor (demote first,
\* so the device list and the armor never disagree) and while it is the
\* combined member.  Access removal touches no root material.
RemovePasskeyDevice ==
    \E pk \in PASSKEYS :
        /\ identity
        /\ role[pk] = "access"
        /\ combined # pk
        /\ role' = [role EXCEPT ![pk] = "unenrolled"]
        /\ pubPublished' = [pubPublished EXCEPT ![pk] = FALSE]
        /\ UNCHANGED <<identity, hasPassword, combined, plainSecretLoc, provOK>>

-------------------------------------------------------------------------------
(* Enable MFA: combine an existing password + passkey into the both-required   *)
(* factor and CLEAR the individual factors.  Requires you already hold both    *)
(* materials (there is always a just-password or just-passkey moment first).   *)

EnableMFA ==
    \E pk \in PASSKEYS :
        /\ AllowUpgradeMFA
        /\ identity
        /\ combined = NoPk
        /\ hasPassword
        /\ role[pk] \in {"access", "root"}
        /\ pubPublished[pk]
        /\ combined' = pk
        /\ hasPassword' = IF ClearOnCombine THEN FALSE ELSE hasPassword
        /\ role' = IF ClearOnCombine
                     THEN [q \in PASSKEYS |->
                             IF role[q] = "root" THEN "access" ELSE role[q]]
                     ELSE role
        /\ plainSecretLoc' = SecretEnd
        /\ provOK' = ProvAfter
        /\ UNCHANGED <<identity, pubPublished>>

-------------------------------------------------------------------------------

Next ==
    \/ FoundFreshPassword
    \/ FoundFreshPasskey
    \/ FoundFreshCombined
    \/ EnrollPasskeyAccess
    \/ PromotePasskeyToRoot
    \/ AddPassword
    \/ RemovePassword
    \/ DemotePasskey
    \/ RemovePasskeyDevice
    \/ EnableMFA

Spec == Init /\ [][Next]_vars

-------------------------------------------------------------------------------
(* Invariants. *)

TypeOK ==
    /\ identity \in BOOLEAN
    /\ hasPassword \in BOOLEAN
    /\ role \in [PASSKEYS -> {"unenrolled", "access", "root"}]
    /\ combined \in ({NoPk} \cup PASSKEYS)
    /\ pubPublished \in [PASSKEYS -> BOOLEAN]
    /\ plainSecretLoc \in {"none", "browser", "disk", "python", "relay"}
    /\ provOK \in BOOLEAN

\* Once founded, there is always at least one way to open the root.
RootReachable == identity => OpenerExists

\* MFA is exclusive: the combined factor never coexists with a standalone
\* individual opener.
MFAExclusive ==
    combined # NoPk =>
        /\ ~hasPassword
        /\ \A pk \in PASSKEYS : role[pk] # "root"

\* I1: the plaintext root secret is never persisted to a forbidden location.
SeedNeverPersisted == plainSecretLoc \in {"none", "browser"}

\* Every statement check used the trusted ledger key, never the payload signer.
ProvenanceOK == provOK

\* A passkey cannot be a root factor (or the combined member) unless its
\* provisioning public key was published to the ledger (key-exchange order).
PublishedBeforeAuthority ==
    /\ \A pk \in PASSKEYS : role[pk] = "root" => pubPublished[pk]
    /\ (combined # NoPk => pubPublished[combined])

Inv ==
    /\ TypeOK
    /\ RootReachable
    /\ MFAExclusive
    /\ SeedNeverPersisted
    /\ ProvenanceOK
    /\ PublishedBeforeAuthority

-------------------------------------------------------------------------------
(* Reachability probes.  Each is expected to be VIOLATED — a failing run   *)
(* proves the target state is reachable.  Used only in probe .cfg files.   *)

\* Passkey-only is a valid, reachable configuration.
NoPasskeyOnly ==
    ~(identity /\ ~hasPassword /\ combined = NoPk /\ RootPasskeyExists)

\* Password-only is a valid, reachable configuration.
NoPasswordOnly ==
    ~(identity /\ hasPassword /\ combined = NoPk /\ ~RootPasskeyExists)

\* An MFA (combined) configuration is reachable.  Pair with AllowUpgradeMFA
\* = FALSE to prove the nothing -> combined DIRECT founding path specifically.
NoMFA == combined = NoPk

===============================================================================
