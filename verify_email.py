import smtplib
import dns.resolver
import socket


def _check_one(email_address, sender_email):
    """The actual SMTP handshake probe for a single address. Returns
    (is_valid, message)."""
    domain = email_address.split('@')[-1]

    try:
        # 1. Look up MX records for the domain
        mx_records = dns.resolver.resolve(domain, 'MX')
        mx_record = str(mx_records[0].exchange)
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, Exception) as e:
        return False, f"Domain or MX record not found for {domain}: {e}"

    try:
        # 2. Connect to the mail server via SMTP (port 25)
        server = smtplib.SMTP(timeout=10)
        server.connect(mx_record)
        server.helo(server.local_hostname) # Identify yourself

        # 3. Simulate the sender and recipient handshake
        server.mail(sender_email)
        code, message = server.rcpt(email_address)

        server.quit()

        # 4. Interpret the response code (250 means accepted/valid)
        if code == 250:
            return True, f"Valid: {email_address} (Server accepted the address)"
        else:
            return False, f"Invalid: {email_address} (Rejected with code {code}: {message.decode()})"

    except (socket.error, smtplib.SMTPException) as e:
        return False, f"Connection error or blocked by server: {e}"


def verify_email_smtp(email_address, sender_email="verify@example.com"):
    """Checks an email address via SMTP. `email_address` may also be a
    comma-delimited list of candidate addresses (as produced by
    lead_gen.generate_email_permutations() for a guessed contact) -- each is
    tried in turn, stopping at the first one the target mail server accepts.

    Always returns a message string. On success it's the same
    "Valid: <email> (Server accepted the address)" format whether one
    address or several candidates were given, so callers can pull the
    winning address out of it the same way either way.
    """
    candidates = [c.strip() for c in email_address.split(",") if c.strip()]
    if not candidates:
        return "No email address given."

    results = []
    for candidate in candidates:
        is_valid, message = _check_one(candidate, sender_email)
        if is_valid:
            return message
        results.append(message)

    if len(candidates) == 1:
        return results[0]
    return f"No valid address found among {len(candidates)} candidate(s):\n" + "\n".join(results)

# Example test
if __name__ == "__main__":
    test_email = "joshua@cowbellcyber.ai"
    print(verify_email_smtp(test_email))