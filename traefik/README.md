To create a new password, use the htpasswd command:  
`docker run --rm httpd:2.4-alpine htpasswd -nbB admin "your-new-password" | cut -d ":" -f 2`  
Then update the `usersfile` with the new user/password combination (one per line)
