import smtplib
import dns.resolver
import socket

def verify_email_smtp(email_address, sender_email="verify@example.com"):
    domain = email_address.split('@')[-1]

    try:
        # 1. Look up MX records for the domain
        mx_records = dns.resolver.resolve(domain, 'MX')
        mx_record = str(mx_records[0].exchange)
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, Exception) as e:
        return f"Domain or MX record not found for {domain}: {e}"

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
            return f"Valid: {email_address} (Server accepted the address)"
        else:
            return f"Invalid: {email_address} (Rejected with code {code}: {message.decode()})"

    except (socket.error, smtplib.SMTPException) as e:
        return f"Connection error or blocked by server: {e}"

# Example test
if __name__ == "__main__":
    test_email = "joshua@cowbellcyber.ai"
    print(verify_email_smtp(test_email))